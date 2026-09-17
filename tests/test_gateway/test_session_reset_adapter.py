from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

import pytest

from opensquilla.application.session_reset import (
    ResetSession,
    SessionResetForcePermissionError,
    SessionResetNotFoundError,
    SessionResetResult,
    SessionResetUnavailableError,
)
from opensquilla.gateway.adapters.session_reset import GatewaySessionResetAdapter
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError


@dataclass
class _Application:
    commands: list[ResetSession] = field(default_factory=list)
    error: Exception | None = None

    async def reset(self, command: ResetSession) -> SessionResetResult:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return SessionResetResult(
            session_key=command.session_key,
            previous_session_id="session-old",
            session_id="session-new",
            rotated=True,
            epoch=3,
        )


async def test_adapter_terminates_force_authority_and_projects_wire_result() -> None:
    application = _Application()
    context = cast(
        RpcContext,
        SimpleNamespace(has_scope=lambda scope: scope == "operator.admin"),
    )
    adapter = GatewaySessionResetAdapter(context, application)

    result = await adapter.reset(
        {"key": "agent:main:webchat:one", "force": True}
    )

    assert application.commands == [
        ResetSession(
            session_key="agent:main:webchat:one",
            force=True,
            force_authorized=True,
        )
    ]
    assert result == {
        "key": "agent:main:webchat:one",
        "reset": True,
        "rotated": True,
        "previous_session_id": "session-old",
        "session_id": "session-new",
        "epoch": 3,
    }


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            SessionResetForcePermissionError(
                session_key="agent:main:webchat:one",
                session_id="session-old",
            ),
            "permission_denied",
        ),
    ],
)
async def test_adapter_projects_typed_reset_failures(
    error: Exception,
    code: str,
) -> None:
    application = _Application(error=error)
    context = cast(RpcContext, SimpleNamespace(has_scope=lambda _scope: False))
    adapter = GatewaySessionResetAdapter(context, application)

    with pytest.raises(RpcHandlerError) as raised:
        await adapter.reset({"key": "agent:main:webchat:one"})

    assert raised.value.code == code
    assert raised.value.details["key"] == "agent:main:webchat:one"
    assert raised.value.details["session_id"] == "session-old"


@pytest.mark.parametrize(
    "error",
    [
        SessionResetUnavailableError(),
        SessionResetNotFoundError("agent:main:webchat:one"),
    ],
)
async def test_adapter_preserves_missing_session_not_found_projection(
    error: Exception,
) -> None:
    application = _Application(error=error)
    context = cast(RpcContext, SimpleNamespace(has_scope=lambda _scope: False))
    adapter = GatewaySessionResetAdapter(context, application)

    with pytest.raises(KeyError):
        await adapter.reset({"key": "agent:main:webchat:one"})
