"""新增 meta 事件被 replay buffer 完整保留，断线重连可补齐。"""

from opensquilla.gateway.session_streams import SessionStreamRegistry


def _record_and_replay(events_to_record, since=0):
    reg = SessionStreamRegistry(max_events_per_session=500)
    for name, payload in events_to_record:
        reg.record("sess1", name, payload)
    return reg.replay("sess1", since)


def test_meta_run_announced_preserved_through_replay():
    result = _record_and_replay([
        ("session.event.meta_run_announced",
         {"run_id": "r1", "meta_skill_name": "x", "total": 2}),
    ])
    assert result.replay_complete is True
    assert len(result.events) == 1
    assert result.events[0].event_name == "session.event.meta_run_announced"
    assert result.events[0].payload["run_id"] == "r1"


def test_meta_step_state_preserved_through_replay():
    result = _record_and_replay([
        ("session.event.meta_run_announced", {"run_id": "r1", "total": 1}),
        ("session.event.meta_step_state",
         {"run_id": "r1", "step_id": "s1", "state": "running"}),
        ("session.event.meta_step_state",
         {"run_id": "r1", "step_id": "s1", "state": "succeeded"}),
    ])
    assert len(result.events) == 3
    states = [
        e.payload["state"]
        for e in result.events
        if e.event_name == "session.event.meta_step_state"
    ]
    assert states == ["running", "succeeded"]


def test_meta_events_survive_buffer_trim_pressure():
    reg = SessionStreamRegistry(max_events_per_session=5)
    # Fill with lossy events (text_delta) first; should be evictable.
    for i in range(10):
        reg.record("s", "session.event.text_delta", {"i": i})
    # Now record meta events; lossy evictions should make room.
    reg.record("s", "session.event.meta_run_announced", {"run_id": "r1"})
    reg.record("s", "session.event.meta_step_state",
               {"run_id": "r1", "step_id": "a", "state": "running"})
    result = reg.replay("s", 10)
    kept = [e.event_name for e in result.events]
    assert "session.event.meta_run_announced" in kept
    assert "session.event.meta_step_state" in kept


def test_evict_clears_only_the_target_session_stream_state():
    reg = SessionStreamRegistry(max_events_per_session=500)
    target = "agent:main:subagent:target-stream"
    sibling = "agent:main:subagent:sibling-stream"

    for session_key, task_id in ((target, "task-target"), (sibling, "task-sibling")):
        reg.record(
            session_key,
            "session.event.provider_activity",
            {"task_id": task_id, "turn_id": task_id, "phase": "requesting"},
        )
        reg.record(
            session_key,
            "session.event.tool_use_start",
            {
                "task_id": task_id,
                "turn_id": task_id,
                "tool_use_id": f"{task_id}-tool",
                "tool_name": "synthetic_tool",
                "arguments": {},
            },
        )
        reg.record(
            session_key,
            "session.event.tool_result",
            {
                "task_id": task_id,
                "turn_id": task_id,
                "tool_use_id": f"{task_id}-tool",
                "tool_name": "synthetic_tool",
                "result": "ok",
            },
        )
        reg.record(
            session_key,
            "session.event.done",
            {"task_id": task_id, "turn_id": task_id},
        )
        reg.record(
            session_key,
            "session.event.state_change",
            {"task_id": f"{task_id}-live", "to_state": "running"},
        )

    reg.evict(target)

    target_replay = reg.replay(target, 0)
    assert target_replay.current_stream_seq == 0
    assert target_replay.events == []
    assert reg.live_snapshot(target).events == []
    assert reg.take_terminal_activity_snapshot(
        target,
        "task-target",
        turn_id="task-target",
    ) is None

    sibling_replay = reg.replay(sibling, 0)
    assert sibling_replay.current_stream_seq == 5
    assert [event.event_name for event in sibling_replay.events] == [
        "session.event.provider_activity",
        "session.event.tool_use_start",
        "session.event.tool_result",
        "session.event.done",
        "session.event.state_change",
    ]
    assert reg.live_snapshot(sibling).task_id == "task-sibling-live"
    assert reg.take_terminal_activity_snapshot(
        sibling,
        "task-sibling",
        turn_id="task-sibling",
    ) is not None
