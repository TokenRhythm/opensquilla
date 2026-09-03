from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionMaintenance,
)


@dataclass
class _Runtime:
    compactions: list[CompactSession] = field(default_factory=list)

    async def compact(self, command: CompactSession) -> dict[str, Any]:
        self.compactions.append(command)
        return {"key": command.session_key, "status": "started"}


async def test_compaction_is_canonicalized_before_runtime_entry() -> None:
    runtime = _Runtime()
    application = SessionMaintenance(runtime)

    await application.compact(
        CompactSession(
            " agent:main:webchat:one ",
            wait=False,
            context_window_tokens=4_096,
            instructions="Preserve active decisions.",
        )
    )

    assert runtime.compactions == [
        CompactSession(
            "agent:main:webchat:one",
            wait=False,
            context_window_tokens=4_096,
            instructions="Preserve active decisions.",
        )
    ]


async def test_invalid_compaction_budget_never_reaches_runtime() -> None:
    runtime = _Runtime()
    application = SessionMaintenance(runtime)

    with pytest.raises(ValueError, match="positive"):
        await application.compact(
            CompactSession("agent:main:webchat:one", context_window_tokens=0)
        )

    assert runtime.compactions == []
