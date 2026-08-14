from __future__ import annotations

import asyncio
from collections import OrderedDict

import pytest

from opensquilla.engine import cache_break_monitor
from opensquilla.engine.cache_break_monitor import CacheBreakMonitor
from opensquilla.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from opensquilla.session.turn_context import current_turn_context, turn_context_scope


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        input_schema=ToolInputSchema(properties={}),
    )


def _isolate_compaction_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_break_monitor, "_compaction_sequences", {})
    monkeypatch.setattr(cache_break_monitor, "_compaction_terminals", OrderedDict())
    monkeypatch.setattr(cache_break_monitor, "_active_compaction_tasks", {})
    monkeypatch.setattr(cache_break_monitor, "_compaction_heartbeat_tasks", {})


def test_cache_break_monitor_initializes_then_detects_attributed_drop() -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05)
    first = monitor.record_prompt_state(
        messages=[
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
            Message(role="user", content="current question"),
        ],
        tools=[_tool("search")],
        config=ChatConfig(
            system="stable system",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
        ),
        model="anthropic/claude-sonnet-4-6",
    )

    initial = monitor.check_response_for_cache_break("agent:main:s1", first, 5000)

    assert initial.break_detected is False
    assert initial.reason == "baseline_initialized"

    second = monitor.record_prompt_state(
        messages=[
            Message(role="user", content="different old question"),
            Message(role="assistant", content="old answer"),
            Message(role="user", content="current question"),
        ],
        tools=[_tool("search")],
        config=ChatConfig(
            system="stable system",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
        ),
        model="anthropic/claude-sonnet-4-6",
    )

    report = monitor.check_response_for_cache_break("agent:main:s1", second, 100)

    assert report.break_detected is True
    assert report.reason == "cache_read_drop"
    assert report.changed_fields == ("messages_prefix_hash",)
    assert report.previous_cache_read_tokens == 5000
    assert report.current_cache_read_tokens == 100
    log_payload = report.to_log_dict()
    assert "forensics" in log_payload
    assert log_payload["forensics"]["previous"]["messages_prefix_item_hashes"]
    assert log_payload["forensics"]["previous"]["messages_prefix_item_kinds"] == ["history"]
    assert log_payload["forensics"]["current"]["cache_control_field_hashes"]


def test_cache_break_monitor_forensics_labels_request_context_prefix_items() -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05)
    first = monitor.record_prompt_state(
        messages=[
            Message(role="user", content="[Request context for this turn]\nvolatile one"),
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
            Message(role="user", content="current question"),
        ],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    monitor.check_response_for_cache_break("agent:main:s1", first, 5000)

    second = monitor.record_prompt_state(
        messages=[
            Message(role="user", content="[Request context for this turn]\nvolatile two"),
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
            Message(role="user", content="current question"),
        ],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )

    report = monitor.check_response_for_cache_break("agent:main:s1", second, 100)

    assert report.break_detected is True
    payload = report.to_log_dict()
    assert payload["forensics"]["previous"]["messages_prefix_item_kinds"][0] == "request_context"
    assert payload["forensics"]["current"]["messages_prefix_item_kinds"][0] == "request_context"


def test_cache_break_monitor_resets_baseline_after_compaction() -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05)
    before = monitor.record_prompt_state(
        messages=[Message(role="user", content="old"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    monitor.check_response_for_cache_break("agent:main:s1", before, 5000)
    monitor.notify_compaction("agent:main:s1")

    after = monitor.record_prompt_state(
        messages=[
            Message(role="assistant", content="kept"),
            Message(role="user", content="now"),
        ],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )

    report = monitor.check_response_for_cache_break("agent:main:s1", after, 0)

    assert report.break_detected is False
    assert report.reason == "baseline_reset_after_compaction"
    assert report.baseline_reset is True


def test_notify_compaction_notifies_registered_listeners() -> None:
    events: list[tuple[str, dict]] = []

    remove = cache_break_monitor.add_compaction_listener(
        lambda session_key, payload: events.append((session_key, payload))
    )
    try:
        cache_break_monitor.notify_compaction(
            "agent:main:s1",
            source="manual",
            phase="manual",
            tokens_before=100,
            tokens_after=40,
        )
    finally:
        remove()

    assert events == [
        (
            "agent:main:s1",
            {
                "status": "completed",
                "source": "manual",
                "phase": "manual",
                "tokens_before": 100,
                "tokens_after": 40,
            },
        )
    ]


def test_notify_compaction_records_durable_automatic_activity_on_current_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_compaction_lifecycle(monkeypatch)
    events: list[dict] = []
    remove = cache_break_monitor.add_compaction_listener(
        lambda _session_key, payload: events.append(payload)
    )
    try:
        with turn_context_scope({"turn_id": "turn-compaction-activity"}):
            started = cache_break_monitor.notify_compaction(
                "agent:main:activity",
                status="started",
                source="automatic",
                compaction_id="compaction-activity-1",
                applied=False,
                durability="none",
            )
            completed = cache_break_monitor.notify_compaction(
                "agent:main:activity",
                status="completed",
                source="automatic",
                compaction_id="compaction-activity-1",
                applied=True,
                durability="durable",
            )
            context = current_turn_context()
    finally:
        remove()

    assert started is not None
    assert completed is not None
    assert started["turn_id"] == "turn-compaction-activity"
    assert started["task_id"] == "turn-compaction-activity"
    assert completed["turn_id"] == "turn-compaction-activity"
    assert completed["task_id"] == "turn-compaction-activity"
    assert context is not None
    assert len(context["activity_markers"]) == 1
    marker = context["activity_markers"][0]
    assert marker == {
        "kind": "context_compaction",
        "id": "compaction-activity-1",
        "status": "completed",
        "at": marker["at"],
    }
    assert isinstance(marker["at"], int)
    assert [event["sequence"] for event in events] == [1, 2]
    assert current_turn_context() is None


def test_notify_compaction_does_not_record_non_durable_or_manual_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_compaction_lifecycle(monkeypatch)

    with turn_context_scope({"turn_id": "turn-compaction-filter"}):
        cache_break_monitor.notify_compaction(
            "agent:main:activity-filter",
            status="completed",
            source="automatic",
            compaction_id="compaction-request-scoped",
            applied=True,
            durability="request_scoped",
        )
        cache_break_monitor.notify_compaction(
            "agent:main:activity-filter",
            status="completed",
            source="manual",
            compaction_id="compaction-manual",
            applied=True,
            durability="durable",
        )

        assert current_turn_context() == {"turn_id": "turn-compaction-filter"}


def test_notify_compaction_resets_cache_only_after_completed_status(
    monkeypatch,
) -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05)
    monkeypatch.setattr(cache_break_monitor, "default_cache_break_monitor", monitor)
    before = monitor.record_prompt_state(
        messages=[Message(role="user", content="old"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    monitor.check_response_for_cache_break("agent:main:s1", before, 5000)

    for status in ("started", "observed", "replayed"):
        cache_break_monitor.notify_compaction("agent:main:s1", status=status)
    after_started = monitor.record_prompt_state(
        messages=[
            Message(role="assistant", content="kept"),
            Message(role="user", content="now"),
        ],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    started_report = monitor.check_response_for_cache_break(
        "agent:main:s1", after_started, 0
    )

    assert started_report.reason != "baseline_reset_after_compaction"

    cache_break_monitor.notify_compaction("agent:main:s1", status="completed")
    after_completed = monitor.record_prompt_state(
        messages=[
            Message(role="assistant", content="new baseline"),
            Message(role="user", content="now"),
        ],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    completed_report = monitor.check_response_for_cache_break(
        "agent:main:s1", after_completed, 0
    )

    assert completed_report.reason == "baseline_reset_after_compaction"


def test_notify_compaction_can_reset_cache_without_notifying_listeners(
    monkeypatch,
) -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05)
    monkeypatch.setattr(cache_break_monitor, "default_cache_break_monitor", monitor)
    events: list[tuple[str, dict]] = []
    remove = cache_break_monitor.add_compaction_listener(
        lambda session_key, payload: events.append((session_key, payload))
    )
    try:
        before = monitor.record_prompt_state(
            messages=[
                Message(role="user", content="old"),
                Message(role="user", content="now"),
            ],
            tools=None,
            config=ChatConfig(system="stable system"),
            model="model-a",
        )
        monitor.check_response_for_cache_break("agent:main:s1", before, 5000)

        cache_break_monitor.notify_compaction(
            "agent:main:s1",
            status="completed",
            notify_listeners=False,
        )

        after = monitor.record_prompt_state(
            messages=[
                Message(role="assistant", content="new baseline"),
                Message(role="user", content="now"),
            ],
            tools=None,
            config=ChatConfig(system="stable system"),
            model="model-a",
        )
        report = monitor.check_response_for_cache_break("agent:main:s1", after, 0)
    finally:
        remove()

    assert events == []
    assert report.reason == "baseline_reset_after_compaction"


@pytest.mark.asyncio
async def test_notify_compaction_sequences_started_heartbeat_terminal_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_compaction_lifecycle(monkeypatch)
    session_key = "agent:main:heartbeat-lifecycle"
    compaction_id = "compaction-heartbeat-lifecycle"
    events: list[dict] = []
    heartbeat_seen = asyncio.Event()

    def _record_event(_session_key: str, payload: dict) -> None:
        events.append(payload)
        if payload.get("heartbeat") is True:
            heartbeat_seen.set()

    remove = cache_break_monitor.add_compaction_listener(_record_event)
    heartbeat_task: asyncio.Task[None] | None = None
    try:
        started = cache_break_monitor.notify_compaction(
            session_key,
            status="started",
            source="manual",
            phase="manual",
            compaction_id=compaction_id,
            heartbeat_interval_seconds=0.01,
        )

        assert started is not None
        assert started["sequence"] == 1
        assert cache_break_monitor.active_compaction_ids(session_key) == (compaction_id,)
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=1.0)

        heartbeat_task = cache_break_monitor._compaction_heartbeat_tasks[
            (session_key, compaction_id)
        ]
        terminal = cache_break_monitor.notify_compaction(
            session_key,
            status="completed",
            source="manual",
            phase="manual",
            compaction_id=compaction_id,
        )
        assert terminal is not None

        await asyncio.wait_for(heartbeat_task, timeout=1.0)
        event_count_after_terminal = len(events)
        await asyncio.sleep(0.03)
    finally:
        remove()
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["status"] == "started"
    assert all(
        event["status"] == "observed" and event.get("heartbeat") is True
        for event in events[1:-1]
    )
    assert events[-1]["status"] == "completed"
    assert len(events) == event_count_after_terminal
    assert cache_break_monitor.active_compaction_ids(session_key) == ()


def test_notify_compaction_delivers_terminal_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_compaction_lifecycle(monkeypatch)
    compaction_id = "compaction-terminal-once"
    events: list[dict] = []
    remove = cache_break_monitor.add_compaction_listener(
        lambda _session_key, payload: events.append(payload)
    )
    try:
        started = cache_break_monitor.notify_compaction(
            "agent:main:terminal-once",
            status="started",
            compaction_id=compaction_id,
        )
        terminal = cache_break_monitor.notify_compaction(
            "agent:main:terminal-once",
            status="completed",
            compaction_id=compaction_id,
        )
        duplicate = cache_break_monitor.notify_compaction(
            "agent:main:terminal-once",
            status="failed",
            compaction_id=compaction_id,
        )
    finally:
        remove()

    assert started is not None
    assert terminal is not None
    assert duplicate is None
    assert [(event["status"], event["sequence"]) for event in events] == [
        ("started", 1),
        ("completed", 2),
    ]
    assert cache_break_monitor.compaction_terminal_status(compaction_id) == "completed"


def test_notify_compaction_treats_stale_as_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_compaction_lifecycle(monkeypatch)
    compaction_id = "compaction-stale-terminal"

    cache_break_monitor.notify_compaction(
        "agent:main:stale-terminal",
        status="started",
        compaction_id=compaction_id,
    )
    terminal = cache_break_monitor.notify_compaction(
        "agent:main:stale-terminal",
        status="stale",
        compaction_id=compaction_id,
    )
    duplicate = cache_break_monitor.notify_compaction(
        "agent:main:stale-terminal",
        status="failed",
        compaction_id=compaction_id,
    )

    assert terminal is not None
    assert duplicate is None
    assert cache_break_monitor.compaction_terminal_status(compaction_id) == "stale"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_owner", "expected_status", "expected_reason"),
    [
        (False, "failed", "owner_task_failed"),
        (True, "cancelled", "owner_task_cancelled"),
    ],
)
async def test_owner_task_exit_backstops_missing_compaction_terminal(
    monkeypatch: pytest.MonkeyPatch,
    cancel_owner: bool,
    expected_status: str,
    expected_reason: str,
) -> None:
    _isolate_compaction_lifecycle(monkeypatch)
    session_key = "agent:main:owner-backstop"
    compaction_id = f"compaction-owner-backstop-{expected_status}"
    events: list[dict] = []
    release = asyncio.Event()

    async def _owner() -> None:
        cache_break_monitor.notify_compaction(
            session_key,
            status="started",
            source="automatic",
            phase="preflight",
            compaction_id=compaction_id,
            heartbeat_interval_seconds=60.0,
        )
        if cancel_owner:
            await release.wait()
        raise RuntimeError("unexpected owner failure")

    remove = cache_break_monitor.add_compaction_listener(
        lambda _session_key, payload: events.append(payload)
    )
    owner = asyncio.create_task(_owner())
    try:
        await asyncio.sleep(0)
        if cancel_owner:
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        remove()
        heartbeat = cache_break_monitor._compaction_heartbeat_tasks.get(
            (session_key, compaction_id)
        )
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    assert [event["status"] for event in events] == ["started", expected_status]
    assert events[-1]["reason"] == expected_reason
    assert cache_break_monitor.compaction_terminal_status(compaction_id) == expected_status
    assert cache_break_monitor.active_compaction_ids(session_key) == ()


@pytest.mark.asyncio
async def test_cancel_active_compactions_is_scoped_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_compaction_lifecycle(monkeypatch)
    release = asyncio.Event()

    async def _wait_for_release() -> None:
        await release.wait()

    first = asyncio.create_task(_wait_for_release())
    second = asyncio.create_task(_wait_for_release())
    other_session = asyncio.create_task(_wait_for_release())
    try:
        cache_break_monitor.register_active_compaction(
            "agent:main:target",
            "compaction-target-1",
            first,
        )
        cache_break_monitor.register_active_compaction(
            "agent:main:target",
            "compaction-target-2",
            second,
        )
        cache_break_monitor.register_active_compaction(
            "agent:main:other",
            "compaction-other",
            other_session,
        )

        cancelled = cache_break_monitor.cancel_active_compactions("agent:main:target")
        await asyncio.gather(*cancelled, return_exceptions=True)

        assert set(cancelled) == {first, second}
        assert first.cancelled() is True
        assert second.cancelled() is True
        assert other_session.done() is False
        assert cache_break_monitor.active_compaction_ids("agent:main:target") == ()
        assert cache_break_monitor.active_compaction_ids("agent:main:other") == (
            "compaction-other",
        )
    finally:
        other_session.cancel()
        await asyncio.gather(other_session, return_exceptions=True)


def _record(monitor: CacheBreakMonitor, session_key: str, tokens: int = 5000) -> None:
    snapshot = monitor.record_prompt_state(
        messages=[
            Message(role="user", content=f"old {session_key}"),
            Message(role="user", content="now"),
        ],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    monitor.check_response_for_cache_break(session_key, snapshot, tokens)


def test_evict_drops_all_cache_break_state_for_one_session() -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05)
    _record(monitor, "agent:main:s1")
    _record(monitor, "agent:main:s2")
    monitor.notify_compaction("agent:main:s1")

    assert monitor.evict("agent:main:s1") is True
    assert monitor.tracked_session_count == 1

    snapshot = monitor.record_prompt_state(
        messages=[Message(role="user", content="fresh"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="different system"),
        model="model-b",
    )
    report = monitor.check_response_for_cache_break("agent:main:s1", snapshot, 0)

    assert report.break_detected is False
    assert report.reason == "baseline_initialized"


def test_evict_reports_false_for_untracked_session() -> None:
    assert CacheBreakMonitor().evict("agent:main:never-seen") is False


@pytest.mark.asyncio
async def test_module_level_eviction_targets_only_default_cache_break_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_compaction_lifecycle(monkeypatch)
    monitor = CacheBreakMonitor()
    monkeypatch.setattr(cache_break_monitor, "default_cache_break_monitor", monitor)
    _record(monitor, "agent:main:s1")

    async def _waiting_owner() -> None:
        await asyncio.Event().wait()

    owner = asyncio.create_task(_waiting_owner())
    cache_break_monitor.register_active_compaction(
        "agent:main:s1",
        "compaction-still-owned",
        owner,
    )
    try:
        assert cache_break_monitor.evict_cache_break_state("agent:main:s1") is True
        assert monitor.tracked_session_count == 0
        assert cache_break_monitor.active_compaction_ids("agent:main:s1") == (
            "compaction-still-owned",
        )
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


def test_tracked_sessions_stay_bounded_without_explicit_eviction() -> None:
    monitor = CacheBreakMonitor(max_sessions=4)

    for index in range(50):
        _record(monitor, f"agent:main:s{index}")

    assert monitor.tracked_session_count == 4


def test_lru_eviction_never_reports_a_false_break() -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05, max_sessions=2)
    _record(monitor, "agent:main:cold", tokens=5000)
    _record(monitor, "agent:main:hot1")
    _record(monitor, "agent:main:hot2")

    changed = monitor.record_prompt_state(
        messages=[Message(role="user", content="changed"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="different system"),
        model="model-b",
    )
    report = monitor.check_response_for_cache_break("agent:main:cold", changed, 0)

    assert report.break_detected is False
    assert report.reason == "baseline_initialized"


def test_most_recently_used_session_survives_the_bound() -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05, max_sessions=2)
    _record(monitor, "agent:main:keep", tokens=5000)
    _record(monitor, "agent:main:filler1")
    _record(monitor, "agent:main:keep", tokens=5000)
    _record(monitor, "agent:main:filler2")

    changed = monitor.record_prompt_state(
        messages=[Message(role="user", content="changed"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="different system"),
        model="model-b",
    )
    report = monitor.check_response_for_cache_break("agent:main:keep", changed, 0)

    assert report.break_detected is True
    assert report.reason == "cache_read_drop"


def test_pending_resets_stay_bounded_without_a_paired_baseline() -> None:
    monitor = CacheBreakMonitor(max_sessions=4)

    for index in range(50):
        monitor.notify_compaction(f"agent:main:ghost{index}")

    assert monitor.tracked_session_count == 4
    snapshot = monitor.record_prompt_state(
        messages=[Message(role="user", content="old"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    report = monitor.check_response_for_cache_break("agent:main:ghost49", snapshot, 0)
    assert report.reason == "baseline_reset_after_compaction"


def test_evict_reports_true_for_a_pending_reset_without_a_baseline() -> None:
    monitor = CacheBreakMonitor()
    monitor.notify_compaction("agent:main:pending-only")

    assert monitor.evict("agent:main:pending-only") is True
    assert monitor.tracked_session_count == 0
    assert monitor.evict("agent:main:pending-only") is False


def test_trimming_never_strands_a_baseline_without_its_reset_marker() -> None:
    monitor = CacheBreakMonitor(min_drop_tokens=10, min_drop_ratio=0.05, max_sessions=2)
    _record(monitor, "agent:main:compacted", tokens=5000)
    monitor.notify_compaction("agent:main:compacted")

    for index in range(10):
        monitor.notify_compaction(f"agent:main:unrelated{index}")

    after_compaction = monitor.record_prompt_state(
        messages=[Message(role="user", content="shrunk"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="rewritten by compaction"),
        model="model-b",
    )
    report = monitor.check_response_for_cache_break(
        "agent:main:compacted",
        after_compaction,
        100,
    )

    assert report.break_detected is False
    assert report.reason == "baseline_initialized"


def test_the_bound_counts_sessions_not_independent_state_maps() -> None:
    monitor = CacheBreakMonitor(max_sessions=3)

    for index in range(20):
        _record(monitor, f"agent:main:with-baseline{index}")
        monitor.notify_compaction(f"agent:main:pending-only{index}")

    assert monitor.tracked_session_count == 3
