from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import opensquilla.gateway.subagent_announce as subagent_announce_module
from opensquilla.gateway.subagent_announce import (
    _build_subagent_group_outcome,
    _build_terminal_group_payloads,
    _format_parent_wake_message,
    _tracker,
    announce_subagent_completion,
    close_subagent_spawn_group,
    set_background_completion_manager,
)
from opensquilla.gateway.task_runtime import SubagentCompletionEvent
from opensquilla.session.models import AgentTaskStatus, SessionStatus

PARENT = "agent:main:webchat:parent"
PARENT_TASK = "task-parent"


class _SessionRow:
    def __init__(
        self,
        session_key: str,
        *,
        status: str,
        agent_id: str = "worker",
    ) -> None:
        self.session_key = session_key
        self.spawned_by = PARENT
        self.parent_session_key = PARENT
        self.agent_id = agent_id
        self.status = status
        self.origin = {"kind": "subagent", "parent_task_id": PARENT_TASK}


class _Storage:
    def __init__(self, tasks_by_session: dict[str, list[SimpleNamespace]]) -> None:
        self.tasks_by_session = tasks_by_session
        self.batch_calls: list[tuple[str, ...]] = []

    async def list_agent_tasks_for_sessions(
        self,
        session_keys: list[str],
        limit_per_session: int = 100,
    ) -> dict[str, list[SimpleNamespace]]:
        self.batch_calls.append(tuple(session_keys))
        return {
            key: list(self.tasks_by_session.get(key, []))[:limit_per_session]
            for key in session_keys
        }


class _SessionManager:
    def __init__(
        self,
        rows: list[_SessionRow],
        *,
        tasks_by_session: dict[str, list[SimpleNamespace]],
        transcripts: dict[str, str],
    ) -> None:
        self.rows = rows
        self._storage = _Storage(tasks_by_session)
        self.transcripts = transcripts
        self.messages: list[tuple[str, str, str, dict | None]] = []
        self.finished: list[tuple[str, str]] = []

    async def list_sessions(
        self,
        agent_id=None,
        status=None,
        limit=100,
        offset=0,
        spawned_by=None,
    ):
        rows = self.rows
        if spawned_by is not None:
            rows = [row for row in rows if row.spawned_by == spawned_by]
        return rows[offset : offset + limit]

    async def read_transcript(self, session_key: str, limit: int = 50):
        text = self.transcripts.get(session_key, "")
        return [SimpleNamespace(role="assistant", content=text)] if text else []

    async def append_message(
        self,
        key: str,
        *,
        role: str,
        content: str,
        provenance: dict | None = None,
    ) -> SimpleNamespace:
        self.messages.append((key, role, content, provenance))
        return SimpleNamespace(message_id=f"parent-message-{len(self.messages)}")

    async def get_session(self, key: str):
        return SimpleNamespace(session_key=key, last_channel=None, last_to=None)

    async def finish(self, session_key: str, *, status: SessionStatus) -> None:
        self.finished.append((session_key, str(status)))
        for row in self.rows:
            if row.session_key == session_key:
                row.status = str(status)


class _TaskRuntime:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    async def send(self, session_key: str, message: str, provenance: dict | None = None):
        self.sent.append((session_key, message, provenance))
        return SimpleNamespace(task_id=f"wake-{len(self.sent)}")


class _BackgroundCompletion:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.waiting: list[dict] = []
        self.wakes: list[dict] = []

    async def emit_waiting(self, **kwargs) -> None:
        self.calls.append("waiting")
        self.waiting.append(kwargs)

    async def send_parent_wake(self, **kwargs) -> None:
        self.calls.append("wake")
        self.wakes.append(kwargs)


@pytest.fixture(autouse=True)
def _clean_tracker():
    _tracker.evict(PARENT)
    set_background_completion_manager(None)
    yield
    _tracker.evict(PARENT)
    set_background_completion_manager(None)


@pytest.mark.asyncio
async def test_completion_event_carries_persisted_message_id_without_polluting_business_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = "agent:worker:subagent:message-id"
    manager = _SessionManager(
        [_SessionRow(child, status="done")],
        tasks_by_session={},
        transcripts={child: "child output"},
    )
    emitted: list[dict] = []
    channel_payloads: list[dict] = []
    wake_payloads: list[dict] = []

    async def emit(_session_key: str, _event_name: str, payload: dict) -> None:
        emitted.append(dict(payload))

    async def capture_channel(payload: dict, **_kwargs) -> None:
        channel_payloads.append(dict(payload))

    async def capture_wake_payloads(_event, payload: dict, _parent_task_id, **_kwargs):
        wake_payloads.append(dict(payload))
        return []

    monkeypatch.setattr(subagent_announce_module, "_announce_to_parent_channel", capture_channel)
    monkeypatch.setattr(
        subagent_announce_module,
        "_build_parent_wake_payloads",
        capture_wake_payloads,
    )
    _tracker.mark_closed(PARENT, PARENT_TASK)
    event = SubagentCompletionEvent(
        parent_session_key=PARENT,
        child_session_key=child,
        task_id="task-child",
        status=AgentTaskStatus.SUCCEEDED,
        terminal_reason="done",
        agent_id="worker",
        parent_task_id=PARENT_TASK,
    )

    await announce_subagent_completion(
        event,
        session_manager=manager,
        event_emitter=emit,
        channel_manager=object(),
        task_runtime=_TaskRuntime(),
    )

    assert emitted[0]["message_id"] == "parent-message-1"
    assert "message_id" not in json.loads(manager.messages[0][2])
    assert "message_id" not in channel_payloads[0]
    assert "message_id" not in wake_payloads[0]


@pytest.mark.asyncio
async def test_group_payloads_enrich_non_current_children_from_task_ledger() -> None:
    child_done = "agent:worker:subagent:done"
    child_failed = "agent:worker:subagent:failed"
    manager = _SessionManager(
        [
            _SessionRow(child_done, status="done", agent_id="worker-a"),
            _SessionRow(child_failed, status="failed", agent_id="worker-b"),
        ],
        tasks_by_session={
            child_done: [
                SimpleNamespace(
                    task_id="task-default-newer",
                    agent_id="worker-a",
                    status=AgentTaskStatus.FAILED,
                    run_kind="default",
                    terminal_reason="followup_error",
                    created_at=100,
                    updated_at=200,
                    finished_at=300,
                ),
                SimpleNamespace(
                    task_id="task-done",
                    agent_id="worker-a",
                    status=AgentTaskStatus.SUCCEEDED,
                    run_kind="subagent",
                    terminal_reason="done",
                    created_at=10,
                    updated_at=20,
                    finished_at=30,
                )
            ],
            child_failed: [
                SimpleNamespace(
                    task_id="task-failed",
                    agent_id="worker-b",
                    status=AgentTaskStatus.FAILED,
                    run_kind="subagent",
                    terminal_reason="tool_error",
                    error_class="RuntimeError",
                    error_message="boom",
                    created_at=11,
                    updated_at=21,
                    finished_at=31,
                )
            ],
        },
        transcripts={child_done: "done result", child_failed: "partial failure details"},
    )

    payloads = await _build_terminal_group_payloads(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        session_manager=manager,
    )

    assert manager._storage.batch_calls == [(child_done, child_failed)]
    assert payloads is not None
    by_child = {payload["child_session_key"]: payload for payload in payloads}
    assert by_child[child_done]["task_id"] == "task-done"
    assert by_child[child_done]["agent_id"] == "worker-a"
    assert by_child[child_done]["status"] == "succeeded"
    assert by_child[child_failed]["task_id"] == "task-failed"
    assert by_child[child_failed]["agent_id"] == "worker-b"
    assert by_child[child_failed]["status"] == "failed"
    assert by_child[child_failed]["terminal_reason"] == "tool_error"
    assert by_child[child_failed]["error_class"] == "RuntimeError"
    assert by_child[child_failed]["error_message"] == "boom"

    wake_message = _format_parent_wake_message(PARENT_TASK, payloads)
    assert "task_id=task-failed" in wake_message
    assert "agent_id=worker-b" in wake_message
    assert "error_class=RuntimeError" in wake_message
    assert "error_message=boom" in wake_message


def test_parent_wake_bounds_aggregate_subagent_output() -> None:
    payloads = []
    for index in range(5):
        payloads.append(
            {
                "child_session_key": f"agent:worker:subagent:{index}",
                "task_id": f"task-{index}",
                "agent_id": f"worker-{index}",
                "status": "succeeded",
                "terminal_reason": "completed",
                "result": {"text": f"result-{index}-" + ("x" * 12_000)},
            }
        )

    wake_message = _format_parent_wake_message(PARENT_TASK, payloads)

    assert len(wake_message) < 20_000
    assert wake_message.count("<untrusted_subagent_result>") == 5
    assert "[subagent result truncated" in wake_message
    assert "child_session_key=agent:worker:subagent:4" in wake_message
    assert "task_id=task-4" in wake_message


def test_group_outcome_summary_counts_and_bounds_non_success_children() -> None:
    long_error = "boom-" * 200
    payloads = [
        {
            "child_session_key": "agent:worker:subagent:done",
            "task_id": "task-done",
            "agent_id": "worker-a",
            "status": "succeeded",
            "terminal_reason": "completed",
        },
        {
            "child_session_key": "agent:worker:subagent:failed",
            "task_id": "task-failed",
            "agent_id": "worker-b",
            "status": "failed",
            "terminal_reason": "tool_error",
            "error_class": "RuntimeError",
            "error_message": long_error,
        },
        {
            "child_session_key": "agent:worker:subagent:timeout",
            "task_id": "task-timeout",
            "agent_id": "worker-c",
            "status": "timeout",
            "terminal_reason": "timeout",
        },
    ]

    outcome = _build_subagent_group_outcome(payloads)

    assert outcome["total"] == 3
    assert outcome["succeeded"] == 1
    assert outcome["failed"] == 1
    assert outcome["timeout"] == 1
    assert outcome["cancelled"] == 0
    assert outcome["abandoned"] == 0
    assert outcome["non_success"] == 2
    assert outcome["runtime_partial_failure_disclosure_required"] is True
    assert [child["child_session_key"] for child in outcome["failed_children"]] == [
        "agent:worker:subagent:failed",
        "agent:worker:subagent:timeout",
    ]
    failed = outcome["failed_children"][0]
    assert failed["task_id"] == "task-failed"
    assert failed["agent_id"] == "worker-b"
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "tool_error"
    assert failed["error_class"] == "RuntimeError"
    assert failed["error_message"].startswith("boom-")
    assert len(failed["error_message"]) < len(long_error)
    assert failed["error_message_truncated"] is True


def test_context_overflow_failure_is_sanitized_for_group_outcome_and_wake() -> None:
    raw_error = (
        "Context overflow is in the current turn's recent tool calls or "
        "reasoning tail; history compaction cannot reduce it."
    )
    payloads = [
        {
            "child_session_key": "agent:worker:subagent:failed",
            "task_id": "task-failed",
            "agent_id": "worker-b",
            "status": "failed",
            "terminal_reason": "error",
            "error_class": "current_turn_context_exhausted",
            "error_message": raw_error,
        }
    ]

    outcome = _build_subagent_group_outcome(payloads)
    failed = outcome["failed_children"][0]
    wake_message = _format_parent_wake_message(PARENT_TASK, payloads, outcome=outcome)

    assert failed["error_class"] == "provider_request_too_large"
    assert "too large" in failed["error_message"].lower()
    assert raw_error not in failed["error_message"]
    assert "current_turn_context_exhausted" not in wake_message
    assert "history compaction cannot reduce it" not in wake_message
    assert "too large" in wake_message.lower()


@pytest.mark.asyncio
async def test_group_payloads_fall_back_when_task_ledger_is_unavailable() -> None:
    child = "agent:worker:subagent:fallback"
    manager = _SessionManager(
        [_SessionRow(child, status="timeout", agent_id="worker")],
        tasks_by_session={},
        transcripts={child: "late output"},
    )
    manager._storage = SimpleNamespace()

    payloads = await _build_terminal_group_payloads(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        session_manager=manager,
    )

    assert payloads == [
        {
            "type": "subagent_completion",
            "parent_session_key": PARENT,
            "child_session_key": child,
            "status": "timeout",
            "terminal_reason": "timeout",
            "parent_task_id": PARENT_TASK,
            "result": {"text": "late output", "truncated": False, "source_role": "assistant"},
            "agent_id": "worker",
        }
    ]


@pytest.mark.asyncio
async def test_parent_wake_is_deferred_until_yield_and_sent_once() -> None:
    child = "agent:worker:subagent:solo"
    manager = _SessionManager(
        [_SessionRow(child, status="done")],
        tasks_by_session={
            child: [
                SimpleNamespace(
                    task_id="task-child",
                    agent_id="worker",
                    status=AgentTaskStatus.SUCCEEDED,
                    run_kind="subagent",
                    terminal_reason="done",
                    created_at=1,
                    updated_at=2,
                    finished_at=3,
                )
            ]
        },
        transcripts={child: "child output"},
    )
    runtime = _TaskRuntime()
    event = SubagentCompletionEvent(
        parent_session_key=PARENT,
        child_session_key=child,
        task_id="task-child",
        status=AgentTaskStatus.SUCCEEDED,
        terminal_reason="done",
        agent_id="worker",
        parent_task_id=PARENT_TASK,
    )

    await announce_subagent_completion(
        event,
        session_manager=manager,
        task_runtime=runtime,
    )
    assert runtime.sent == []
    assert manager.messages

    assert await close_subagent_spawn_group(
        PARENT,
        PARENT_TASK,
        session_manager=manager,
        task_runtime=runtime,
    )
    assert len(runtime.sent) == 1
    assert "[SUBAGENT_COMPLETION_GROUP]" in runtime.sent[0][1]
    assert "task_id=task-child" in runtime.sent[0][1]

    await announce_subagent_completion(
        event,
        session_manager=manager,
        task_runtime=runtime,
    )
    assert len(runtime.sent) == 1

    assert await close_subagent_spawn_group(
        PARENT,
        PARENT_TASK,
        session_manager=manager,
        task_runtime=runtime,
    )
    assert len(runtime.sent) == 1


@pytest.mark.asyncio
async def test_parent_wake_provenance_carries_group_outcome_for_mixed_children() -> None:
    child_done = "agent:worker:subagent:done"
    child_failed = "agent:worker:subagent:failed"
    manager = _SessionManager(
        [
            _SessionRow(child_done, status="done", agent_id="worker-a"),
            _SessionRow(child_failed, status="failed", agent_id="worker-b"),
        ],
        tasks_by_session={
            child_done: [
                SimpleNamespace(
                    task_id="task-done",
                    agent_id="worker-a",
                    status=AgentTaskStatus.SUCCEEDED,
                    run_kind="subagent",
                    terminal_reason="completed",
                    created_at=1,
                    updated_at=2,
                    finished_at=3,
                )
            ],
            child_failed: [
                SimpleNamespace(
                    task_id="task-failed",
                    agent_id="worker-b",
                    status=AgentTaskStatus.FAILED,
                    run_kind="subagent",
                    terminal_reason="tool_error",
                    error_class="RuntimeError",
                    error_message="boom",
                    created_at=1,
                    updated_at=2,
                    finished_at=3,
                )
            ],
        },
        transcripts={child_done: "done result", child_failed: "failed details"},
    )
    runtime = _TaskRuntime()

    assert await close_subagent_spawn_group(
        PARENT,
        PARENT_TASK,
        session_manager=manager,
        task_runtime=runtime,
    )

    assert len(runtime.sent) == 1
    _, wake_message, provenance = runtime.sent[0]
    assert "Subagents: 1/2 succeeded" in wake_message
    assert provenance is not None
    assert provenance["runtime_partial_failure_disclosure_required"] is True
    outcome = provenance["subagent_group_outcome"]
    assert outcome["total"] == 2
    assert outcome["succeeded"] == 1
    assert outcome["failed"] == 1
    assert outcome["non_success"] == 1
    assert outcome["failed_children"] == [
        {
            "child_session_key": child_failed,
            "task_id": "task-failed",
            "agent_id": "worker-b",
            "status": "failed",
            "terminal_reason": "tool_error",
            "error_class": "RuntimeError",
            "error_message": "boom",
            "error_message_truncated": False,
        }
    ]


@pytest.mark.asyncio
async def test_close_emits_waiting_when_spawn_group_is_not_terminal() -> None:
    child = "agent:worker:subagent:running"
    manager = _SessionManager(
        [_SessionRow(child, status="running")],
        tasks_by_session={},
        transcripts={},
    )
    runtime = _TaskRuntime()
    background = _BackgroundCompletion()
    set_background_completion_manager(background)

    closed = await close_subagent_spawn_group(
        PARENT,
        PARENT_TASK,
        session_manager=manager,
        task_runtime=runtime,
    )

    assert closed is False
    assert runtime.sent == []
    assert background.waiting == [
        {
            "parent_session_key": PARENT,
            "parent_task_id": PARENT_TASK,
            "pending_count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_close_uses_background_completion_manager_for_parent_wake() -> None:
    child = "agent:worker:subagent:solo"
    manager = _SessionManager(
        [_SessionRow(child, status="done")],
        tasks_by_session={},
        transcripts={child: "child output"},
    )
    runtime = _TaskRuntime()
    background = _BackgroundCompletion()
    set_background_completion_manager(background)

    assert await close_subagent_spawn_group(
        PARENT,
        PARENT_TASK,
        session_manager=manager,
        task_runtime=runtime,
    )

    assert runtime.sent == []
    assert background.calls == ["waiting", "wake"]
    assert len(background.wakes) == 1
    assert background.waiting == [
        {
            "parent_session_key": PARENT,
            "parent_task_id": PARENT_TASK,
            "pending_count": 0,
        }
    ]
    wake = background.wakes[0]
    assert wake["parent_session_key"] == PARENT
    assert wake["parent_task_id"] == PARENT_TASK
    assert wake["task_runtime"] is runtime
    assert "[SUBAGENT_COMPLETION_GROUP]" in wake["message"]
