from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.application.turn_admission import (
    AdmitTurn,
    CancelTurn,
    PendingInputGuard,
    SteerTurn,
    TurnAdmission,
)


@dataclass
class _Runtime:
    admissions: list[AdmitTurn] = field(default_factory=list)
    cancellations: list[CancelTurn] = field(default_factory=list)
    steers: list[SteerTurn] = field(default_factory=list)

    async def admit(self, command: AdmitTurn) -> dict[str, Any]:
        self.admissions.append(command)
        return {"key": command.session_key, "status": "accepted"}

    async def cancel(self, command: CancelTurn) -> dict[str, Any]:
        self.cancellations.append(command)
        return {"key": command.session_key, "aborted": True}

    async def steer(self, command: SteerTurn) -> dict[str, Any]:
        self.steers.append(command)
        return {"key": command.session_key, "accepted": True}


async def test_canonical_and_legacy_commands_share_one_application_entry() -> None:
    runtime = _Runtime()
    application = TurnAdmission(runtime)

    await application.admit(
        AdmitTurn(" agent:main:webchat:one ", "hello", "webchat", {"intent": "continue"})
    )
    await application.admit(
        AdmitTurn(" agent:main:webchat:one ", "hello", "session", {"intent": "continue"})
    )
    await application.steer(
        SteerTurn(" agent:main:webchat:one ", "guide", "durable", {})
    )
    await application.steer(
        SteerTurn(" agent:main:webchat:one ", "guide", "legacy", {})
    )

    assert {command.surface for command in runtime.admissions} == {"webchat", "session"}
    assert {command.mode for command in runtime.steers} == {"durable", "legacy"}
    assert all(
        command.session_key == "agent:main:webchat:one"
        for command in (*runtime.admissions, *runtime.steers)
    )


async def test_exact_task_cancel_fails_closed_before_runtime() -> None:
    runtime = _Runtime()
    application = TurnAdmission(runtime)

    result = await application.cancel(
        CancelTurn(
            "agent:main:webchat:one",
            "webchat",
            task_id=None,
            task_scoped=True,
            source="webui_abort",
            attributes={},
        )
    )

    assert result == {
        "aborted": False,
        "key": "agent:main:webchat:one",
        "reason": "task_id_required",
    }
    assert runtime.cancellations == []


async def test_pending_steer_requires_complete_atomic_guard() -> None:
    runtime = _Runtime()
    application = TurnAdmission(runtime)

    with pytest.raises(ValueError, match="source scope"):
        await application.steer(
            SteerTurn(
                "agent:main:webchat:one",
                "guide",
                "durable",
                {},
                pending_input=PendingInputGuard("pending-1", "fingerprint", 2),
            )
        )

    assert runtime.steers == []
