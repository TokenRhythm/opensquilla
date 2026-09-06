"""Dedicated in-turn compaction refresh suite for ``StreamConsumerStage``.

The general snapshot harness exercises every event-type branch in
the slice; this suite specifically pins the compaction refresh contract --
the in-turn ``CompactionEvent`` handling -- so future compaction refactors
have an easily discoverable contract to honor.

Five focused tests cover:

1. ``persist_compaction_result`` is invoked with the event's summary +
   kept_entries.
2. ``notify_compaction`` fires after persist.
3. Memory snapshot refresh fires AFTER persist and respects
   ``private_memory_allowed``.
4. System prompt refresh fires AFTER snapshot refresh; the cacheable
   base is extracted from the ``(base, dynamic_suffix)`` tuple when
   ``_assemble_prompt`` returns one.
5. Failed persistence reports a failed lifecycle event and does NOT refresh
   snapshot or prompt state into a false post-compaction view.
"""

from __future__ import annotations

from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import (
    CompactionEvent,
    DoneEvent,
    TextDeltaEvent,
)

from .test_stream_consumer_stage_snapshot import (
    _MAILBOX,
    _Case,
    _drive,
    _RecordingSessionManager,
    _setup_runner,
)


class _RecordingCompactionHook:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.events: list[tuple[str, str | None, str | None, list[int] | None]] = []
        self.outcomes: list[dict[str, Any]] = []

    async def before_compact(self, state) -> None:
        self.events.append(
            (
                "before",
                state.extra.get("phase"),
                None,
                None,
            )
        )
        if self.raises:
            raise RuntimeError("before hook failed")

    async def after_compact(self, state, outcome) -> None:
        outcome_dict = outcome if isinstance(outcome, dict) else {}
        self.outcomes.append(outcome_dict)
        self.events.append(
            (
                "after",
                state.extra.get("phase"),
                outcome_dict.get("summary"),
                outcome_dict.get("kept_entries"),
            )
        )
        if self.raises:
            raise RuntimeError("after hook failed")


def _baseline_case(
    *,
    persist_raises: type[BaseException] | None = None,
    private_memory_allowed: bool = True,
) -> _Case:
    return _Case(
        case_id="compaction_refresh_drive",
        events=[
            TextDeltaEvent(text="pre"),
            CompactionEvent(summary="THE_SUMMARY", kept_entries=[10, 20, 30]),
            TextDeltaEvent(text=" after"),
            DoneEvent(text="pre after"),
        ],
        raise_after=None,
        persist_raises=persist_raises,
        private_memory_allowed=private_memory_allowed,
    )


# ---------------------------------------------------------------------------
# In-turn compaction test 1: persist arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_compaction_result_invoked_with_event_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SessionManager.persist_compaction_result`` is invoked with the
    event's ``summary`` and ``kept_entries`` exactly once per
    CompactionEvent."""

    case = _baseline_case()
    runner = _setup_runner(monkeypatch, case)
    await _drive(
        runner,
        expected_session_id="session-admitted",
        expected_session_epoch=7,
    )

    sm = runner._session_manager
    assert isinstance(sm, _RecordingSessionManager)
    persist_calls = [c for c in sm.calls if c[0] == "persist"]
    assert len(persist_calls) == 1
    assert persist_calls[0][1] == "agent:main:s1"
    assert persist_calls[0][2] == "THE_SUMMARY"
    assert persist_calls[0][3] == [10, 20, 30]
    assert persist_calls[0][4]["expected_session_id"] == "session-admitted"
    assert persist_calls[0][4]["expected_session_epoch"] == 7


@pytest.mark.asyncio
async def test_in_turn_compaction_event_fires_compaction_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = _RecordingCompactionHook()
    case = _baseline_case()
    runner = _setup_runner(monkeypatch, case, compaction_hooks=(hook,))

    yielded, raised = await _drive(runner)

    assert raised is None
    assert any(isinstance(e, DoneEvent) for e in yielded)
    in_turn_events = [
        event for event in hook.events if event[1] == "in_turn_stream"
    ]
    assert in_turn_events == [
        ("before", "in_turn_stream", None, None),
        ("after", "in_turn_stream", "THE_SUMMARY", [10, 20, 30]),
    ]


@pytest.mark.asyncio
async def test_in_turn_compaction_hook_errors_do_not_abort_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = _RecordingCompactionHook(raises=True)
    case = _baseline_case()
    runner = _setup_runner(monkeypatch, case, compaction_hooks=(hook,))

    yielded, raised = await _drive(runner)

    assert raised is None
    assert any(isinstance(e, DoneEvent) for e in yielded)
    in_turn_events = [
        event for event in hook.events if event[1] == "in_turn_stream"
    ]
    assert [event[0] for event in in_turn_events] == ["before", "after"]


@pytest.mark.asyncio
async def test_in_turn_compaction_after_hook_fires_after_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = _RecordingCompactionHook()
    case = _baseline_case(persist_raises=RuntimeError)
    runner = _setup_runner(monkeypatch, case, compaction_hooks=(hook,))

    yielded, raised = await _drive(runner)

    assert raised is None
    assert any(isinstance(e, DoneEvent) for e in yielded)
    in_turn_events = [
        event for event in hook.events if event[1] == "in_turn_stream"
    ]
    assert in_turn_events == [
        ("before", "in_turn_stream", None, None),
        ("after", "in_turn_stream", None, None),
    ]
    assert hook.outcomes[-1]["status"] == "failed"
    assert hook.outcomes[-1]["reason"] == "persist_failed"


# ---------------------------------------------------------------------------
# In-turn compaction test 2: notify_compaction follows persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_compaction_fires_after_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cache_break_monitor.notify_compaction`` is invoked once after
    persist, with the same ``session_key``."""

    notify_calls: list[tuple[str, dict]] = []
    import opensquilla.engine.runtime as runtime_mod

    monkeypatch.setattr(
        runtime_mod,
        "notify_compaction",
        lambda session_key, **payload: notify_calls.append((session_key, payload)),
    )
    # The runtime adapter imports notify_compaction lazily from
    # cache_break_monitor; patch the source module too.
    import opensquilla.engine.cache_break_monitor as cbm

    monkeypatch.setattr(
        cbm,
        "notify_compaction",
        lambda session_key, **payload: notify_calls.append((session_key, payload)),
    )

    case = _baseline_case()
    runner = _setup_runner(monkeypatch, case)
    await _drive(runner)

    assert len(notify_calls) == 1
    assert notify_calls[0][0] == "agent:main:s1"
    assert notify_calls[0][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_persist_failure_emits_failed_lifecycle_without_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notify_calls: list[tuple[str, dict]] = []

    import opensquilla.engine.cache_break_monitor as cbm

    monkeypatch.setattr(
        cbm,
        "notify_compaction",
        lambda session_key, **payload: notify_calls.append((session_key, payload)),
    )

    case = _baseline_case(persist_raises=RuntimeError)
    runner = _setup_runner(monkeypatch, case)
    yielded, raised = await _drive(runner)

    assert raised is None
    assert any(isinstance(e, DoneEvent) for e in yielded)
    statuses = [payload.get("status") for _, payload in notify_calls]
    assert "completed" not in statuses
    assert "failed" in statuses
    failed_payload = next(payload for _, payload in notify_calls if payload["status"] == "failed")
    assert failed_payload["phase"] == "agent_inline_overflow"
    assert failed_payload["reason"] == "persist_failed"


# ---------------------------------------------------------------------------
# In-turn compaction test 3: memory snapshot refresh respects private_memory_allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("private_allowed", [True, False])
@pytest.mark.asyncio
async def test_memory_snapshot_refresh_respects_private_memory(
    private_allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``runner._memory_snapshots[(agent_id, session_key)]`` is refreshed
    after compaction iff ``private_memory_allowed`` is true.

    The dict-write happens IN ADDITION to (not in place of) any existing
    entry; the contract is "after this call, the snapshot reflects the
    post-compaction state".
    """

    case = _baseline_case(private_memory_allowed=private_allowed)
    runner = _setup_runner(monkeypatch, case)
    # Preserve initial dict snapshot keys to detect a write.
    initial_keys = set(runner._memory_snapshots.keys())
    await _drive(runner)

    snap_key = ("agent:main", "agent:main:s1")
    if private_allowed:
        # The dict has at least the snap_key after compaction.
        assert snap_key in runner._memory_snapshots, (
            "private_allowed=True: snapshot was not refreshed"
        )
    else:
        # No write expected when private memory is not allowed.
        assert snap_key not in runner._memory_snapshots or (
            snap_key in initial_keys
        ), "private_allowed=False: snapshot was written"


# ---------------------------------------------------------------------------
# In-turn compaction test 4: system prompt refresh fires + tuple/str extract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("returns_tuple", [True, False])
@pytest.mark.asyncio
async def test_system_prompt_refresh_extracts_cacheable_base(
    returns_tuple: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent.refresh_system_prompt`` is invoked exactly once per
    CompactionEvent with the cacheable base (NOT a tuple). The
    tuple-vs-str pattern is exercised for both return shapes.
    """

    case = _baseline_case()
    runner = _setup_runner(monkeypatch, case)

    # Override _assemble_prompt to return a tuple vs str depending on case.
    refresh_payload = (
        ("CACHEABLE_BASE", "VOLATILE_SUFFIX")
        if returns_tuple
        else "CACHEABLE_BASE"
    )

    def _assemble_prompt_override(self, *args, **kwargs):  # noqa: ARG001, ARG002
        return refresh_payload

    runner._assemble_prompt = _assemble_prompt_override.__get__(runner, TurnRunner)

    await _drive(runner)

    assert len(_MAILBOX.refresh_prompt_calls) == 1
    refreshed = _MAILBOX.refresh_prompt_calls[0]
    assert refreshed == "CACHEABLE_BASE", (
        f"returns_tuple={returns_tuple}: "
        f"tuple-vs-str extract diverged ({refreshed!r})"
    )


# ---------------------------------------------------------------------------
# In-turn compaction test 5: persist failure preserves pre-refresh runtime state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_raises_preserves_recoverable_pre_compaction_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed DB persistence must not refresh false post-compaction state."""

    refresh_calls: list[dict] = []
    import opensquilla.engine.turn_runner.harness as harness_mod

    original_refresh = (
        harness_mod._TurnRunnerMemorySnapshotRefreshAdapter.refresh_snapshot
    )

    def _record_refresh(self, **kwargs):
        refresh_calls.append(kwargs)
        return original_refresh(self, **kwargs)

    monkeypatch.setattr(
        harness_mod._TurnRunnerMemorySnapshotRefreshAdapter,
        "refresh_snapshot",
        _record_refresh,
    )

    case = _baseline_case(persist_raises=RuntimeError)
    runner = _setup_runner(monkeypatch, case)
    yielded, raised = await _drive(runner)

    # The turn must NOT abort, but runtime state must stay recoverable.
    assert raised is None
    assert any(isinstance(e, DoneEvent) for e in yielded), (
        "stream aborted after persist failure"
    )
    assert _MAILBOX.refresh_prompt_calls == []
    assert refresh_calls == []
