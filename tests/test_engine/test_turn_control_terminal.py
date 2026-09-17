from __future__ import annotations

import pytest

from opensquilla.contracts.turn_execution import (
    ProviderAdmissionError,
    TurnExecutionContext,
    TurnIdentity,
)
from opensquilla.engine.runtime import _control_terminal_reason_for_exception
from opensquilla.engine.turn_runner.context import control_terminal_event_for_context
from opensquilla.engine.types import ControlTerminalReason


def test_control_terminal_bridge_emits_once_with_same_message_identity() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-control-bridge",
            "assistant-control-bridge",
            "agent:main:control-bridge",
        )
    )

    event = control_terminal_event_for_context(
        context,
        ControlTerminalReason.CANCEL,
    )

    assert event is not None
    assert event.turn_id == "turn-control-bridge"
    assert event.assistant_message_id == "assistant-control-bridge"
    assert event.reason is ControlTerminalReason.CANCEL
    assert event.terminal is True
    assert event.preserve_completed_tools is True
    assert control_terminal_event_for_context(context, event.reason) is None


@pytest.mark.parametrize(
    ("deadline", "control", "expected"),
    [
        (99.0, None, ControlTerminalReason.HARD_DEADLINE),
        (None, lambda: True, ControlTerminalReason.CANCEL),
    ],
)
def test_provider_admission_control_failure_never_looks_like_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
    deadline: float | None,
    control: object,
    expected: ControlTerminalReason,
) -> None:
    monkeypatch.setattr("opensquilla.engine.runtime.time.monotonic", lambda: 100.0)
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-control-classify",
            "assistant-control-classify",
            "agent:main:control-classify",
        ),
        control=control,
        deadline=deadline,
    )

    reason = _control_terminal_reason_for_exception(
        ProviderAdmissionError("turn control state rejects provider admission"),
        context,
        None,
        None,
    )

    assert reason is expected
