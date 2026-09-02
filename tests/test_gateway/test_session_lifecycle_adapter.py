from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from opensquilla.application.session_lifecycle import (
    CreatedSession,
    CreateSession,
    DeleteSessions,
    DeleteSessionsResult,
    ForkedSession,
    SessionForkMode,
)
from opensquilla.gateway.adapters.session_lifecycle import (
    GatewaySessionLifecycleAdapter,
    GatewaySessionLifecycleCallbacks,
    created_session_to_v4,
    forked_session_to_v4,
)
from opensquilla.gateway.rpc import RpcContext


def _unused(*args: object, **kwargs: object) -> Any:
    raise AssertionError(f"unexpected Adapter callback: {args!r} {kwargs!r}")


class _CapturingApplication:
    def __init__(self) -> None:
        self.created: CreateSession | None = None
        self.deleted: DeleteSessions | None = None

    async def create(self, command: CreateSession) -> CreatedSession:
        self.created = command
        return CreatedSession("agent:main:webchat:new", "session-id")

    async def delete(self, command: DeleteSessions) -> DeleteSessionsResult:
        self.deleted = command
        return DeleteSessionsResult((), ())


def _adapter(application: _CapturingApplication) -> GatewaySessionLifecycleAdapter:
    callbacks = GatewaySessionLifecycleCallbacks(
        deployment_fields=lambda _values: (False, None, False, None),
        new_session_key=cast(Any, _unused),
        normalize_agent_id=lambda value: cast(str, value),
        agent_model=cast(Any, _unused),
        agent_exists=cast(Any, _unused),
        validate_deployment=cast(Any, _unused),
        raise_deployment_model_required=cast(Any, _unused),
        require_key=cast(Any, _unused),
        optional_string=cast(Any, _unused),
        optional_non_empty_aliased_string=cast(Any, _unused),
        model_value=lambda value: cast(str | None, value),
        effective_agent_id=cast(Any, _unused),
        fork_session=cast(Any, _unused),
        rename_session=cast(Any, _unused),
        delete_session=cast(Any, _unused),
        emit_session_event=cast(Any, _unused),
        resolve_project_workspace=cast(Any, _unused),
    )
    context = RpcContext(conn_id="test")
    context.session_manager = SimpleNamespace(_storage=object())
    adapter = GatewaySessionLifecycleAdapter(context, callbacks)
    adapter._application = cast(Any, application)
    return adapter


async def test_create_observes_malformed_display_name_without_stringifying_it() -> None:
    application = _CapturingApplication()
    adapter = _adapter(application)

    await adapter.create({"agentId": "main", "displayName": 42})

    assert application.created is not None
    assert application.created.display_name == 42


async def test_delete_preserves_malformed_keys_for_legacy_fail_closed_behavior() -> None:
    application = _CapturingApplication()
    adapter = _adapter(application)

    await adapter.delete({"keys": [42]})

    assert application.deleted is not None
    assert application.deleted.keys == (42,)


def test_gateway_projection_exclusively_owns_wire_field_names() -> None:
    assert created_session_to_v4(
        CreatedSession(
            key="agent:main:webchat:new",
            session_id="session-id",
            seeded_message=True,
        )
    ) == {
        "key": "agent:main:webchat:new",
        "sessionId": "session-id",
        "seededMessage": True,
    }
    assert forked_session_to_v4(
        ForkedSession(
            key="agent:main:webchat:child",
            parent_key="agent:main:webchat:parent",
            mode=SessionForkMode.THROUGH_TURN,
            through_turn_id="turn-7",
        )
    ) == {
        "key": "agent:main:webchat:child",
        "parentKey": "agent:main:webchat:parent",
        "forkMode": "through_turn",
        "throughTurnId": "turn-7",
    }
