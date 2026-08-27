from opensquilla.turn_outcome_projection import (
    FORK_TERMINAL_OUTCOME_CONTEXT_KEY,
    attach_fork_terminal_outcome_projection,
    build_fork_terminal_outcome_projection,
    extract_fork_terminal_outcome_projection,
)


def _snapshot(*, task_id: str = "turn-1", turn_id: str = "turn-1") -> dict:
    return {
        "version": 2,
        "task_id": task_id,
        "turn_id": turn_id,
        "complete": True,
        "reasoning_utf16_length": 0,
        "entries": [
            {
                "type": "phase",
                "id": "provider:requesting:4",
                "order": 4,
                "kind": "provider",
                "phase": "requesting",
                "at": 1_000,
                "ended_at": 2_000,
            }
        ],
    }


def test_v2_fork_projection_carries_an_identity_bound_snapshot() -> None:
    activity = _snapshot()
    projection = build_fork_terminal_outcome_projection(
        session_id="child-id",
        session_key="agent:main:webchat:child",
        turn_id="turn-1",
        task_id="turn-1",
        status="succeeded",
        started_at=1_000,
        finished_at=2_000,
        outcome={"kind": "completed"},
        activity_snapshot=activity,
    )
    activity["entries"][0]["order"] = 999
    context = attach_fork_terminal_outcome_projection(
        {"turn_id": "turn-1"},
        projection,
    )

    restored = extract_fork_terminal_outcome_projection(
        context,
        session_id="child-id",
        session_key="agent:main:webchat:child",
        turn_id="turn-1",
    )

    assert restored is not None
    assert restored["activity_snapshot"]["entries"][0]["order"] == 4
    assert projection["version"] == 2


def test_fork_projection_rejects_mismatched_or_legacy_injected_snapshot() -> None:
    projection = build_fork_terminal_outcome_projection(
        session_id="child-id",
        session_key="agent:main:webchat:child",
        turn_id="turn-1",
        task_id="turn-1",
        status="succeeded",
        started_at=1_000,
        finished_at=2_000,
        outcome={"kind": "completed"},
        activity_snapshot=_snapshot(task_id="other-task"),
    )
    assert "activity_snapshot" not in projection

    legacy = {
        **projection,
        "version": 1,
        "activity_snapshot": _snapshot(),
    }
    restored = extract_fork_terminal_outcome_projection(
        {FORK_TERMINAL_OUTCOME_CONTEXT_KEY: legacy, "turn_id": "turn-1"},
        session_id="child-id",
        session_key="agent:main:webchat:child",
        turn_id="turn-1",
    )
    assert restored is not None
    assert "activity_snapshot" not in restored
