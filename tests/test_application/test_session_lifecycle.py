from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from opensquilla.application.session_lifecycle import (
    CreateSession,
    DeleteSessions,
    ForkSession,
    ForkSessionSpec,
    NewSession,
    OptionalSessionBinding,
    RenameSession,
    SessionAvailabilityRequirement,
    SessionCreationKind,
    SessionDisplayNameError,
    SessionForked,
    SessionForkMode,
    SessionForkPoint,
    SessionIdentity,
    SessionLifecycle,
    SessionLifecycleUnavailableError,
    SessionModelRequest,
    SessionWorkspaceBinding,
)


@dataclass
class _CreationPolicy:
    events: list[str]
    default: str | None = "registry-model"

    def new_session_key(self, agent_id: str, kind: SessionCreationKind) -> str:
        self.events.append(f"key:{agent_id}:{kind.value}")
        return f"agent:{agent_id}:{kind.value}:child"

    async def default_model(self, agent_id: str) -> str | None:
        self.events.append(f"default:{agent_id}")
        return self.default

    async def agent_exists(self, agent_id: str) -> bool:
        self.events.append(f"exists:{agent_id}")
        return True

    def validate_deployment(
        self,
        *,
        session_key: str,
        provider: str | None,
        model: str | None,
        auth_profile: str | None,
    ) -> None:
        self.events.append(
            f"deployment:{session_key}:{provider}:{model}:{auth_profile}"
        )

    async def resolve_workspace(self, workspace_id: str) -> SessionWorkspaceBinding:
        self.events.append(f"workspace:{workspace_id}")
        return SessionWorkspaceBinding(
            workspace_id=workspace_id,
            path="/synthetic/workspace",
            run_mode="safe",
            run_mode_source="project_default",
        )


@dataclass
class _Store:
    events: list[str]
    available: bool = True
    created: NewSession | None = None
    forked: ForkSessionSpec | None = None

    async def create(self, session: NewSession) -> SessionIdentity:
        self.events.append("create")
        self.created = session
        return SessionIdentity(session.session_key, "session-id")

    async def append_initial_user_message(self, session_key: str, message: str) -> None:
        self.events.append(f"seed:{session_key}:{message}")

    async def rename(self, session_key: str, display_name: str) -> None:
        self.events.append(f"rename:{session_key}:{display_name}")

    async def fork_agent_id(self, parent_key: str) -> str:
        self.events.append(f"parent:{parent_key}")
        return "main"

    async def fork(self, spec: ForkSessionSpec) -> SessionIdentity:
        self.events.append("fork")
        self.forked = spec
        return SessionIdentity(spec.child_key, "fork-id")


@dataclass
class _Deletion:
    events: list[str]
    available: bool = True
    failures: set[str] = field(default_factory=set)

    async def delete_one(self, canonical_key: str) -> None:
        self.events.append(f"delete:{canonical_key}")
        if canonical_key in self.failures:
            raise RuntimeError("synthetic delete failure")


@dataclass
class _Events:
    events: list[str]
    published: list[SessionForked] = field(default_factory=list)

    async def publish_forked(self, event: SessionForked) -> None:
        self.events.append("publish")
        self.published.append(event)


def _application(
    events: list[str],
    *,
    policy: _CreationPolicy | None = None,
    store: _Store | None = None,
    deletion: _Deletion | None = None,
    publisher: _Events | None = None,
) -> SessionLifecycle:
    return SessionLifecycle(
        creation_policy=policy or _CreationPolicy(events),
        store=store or _Store(events),
        deletion=deletion or _Deletion(events),
        events=publisher or _Events(events),
    )


async def test_create_owns_model_precedence_workspace_and_seed_ordering() -> None:
    events: list[str] = []
    store = _Store(events)
    application = _application(events, store=store)

    result = await application.create(
        CreateSession(
            agent_id="main",
            display_name="  preserved wire value  ",
            initial_message="hello",
            model=SessionModelRequest(value=None),
            kind=SessionCreationKind.WEBCHAT,
            workspace_id="workspace-1",
            provider=OptionalSessionBinding(),
            auth_profile=OptionalSessionBinding(),
        )
    )

    assert result.seeded_message is True
    assert store.created is not None
    assert store.created.model == "registry-model"
    assert store.created.display_name == "  preserved wire value  "
    assert events == [
        "key:main:webchat",
        "default:main",
        "exists:main",
        "workspace:workspace-1",
        "create",
        "seed:agent:main:webchat:child:hello",
    ]


async def test_create_validates_explicit_deployment_before_availability() -> None:
    events: list[str] = []
    store = _Store(events, available=False)
    application = _application(events, store=store)

    with pytest.raises(SessionLifecycleUnavailableError) as raised:
        await application.create(
            CreateSession(
                agent_id="main",
                display_name=None,
                initial_message=None,
                model=SessionModelRequest(
                    value="model-a",
                    explicitly_supplied=True,
                    string_supplied=True,
                ),
                kind=SessionCreationKind.DEFAULT,
                workspace_id=None,
                provider=OptionalSessionBinding(True, "provider-a"),
                auth_profile=OptionalSessionBinding(),
            )
        )

    assert raised.value.requirement is SessionAvailabilityRequirement.CREATE_WITH_DEPLOYMENT

    assert events[:3] == [
        "key:main:default",
        "deployment:agent:main:default:child:provider-a:model-a:None",
        "exists:main",
    ]


async def test_fork_projects_discriminant_and_publishes_after_persistence() -> None:
    events: list[str] = []
    store = _Store(events)
    publisher = _Events(events)
    application = _application(events, store=store, publisher=publisher)

    result = await application.fork(
        ForkSession(
            parent_key=" agent:main:webchat:parent ",
            title=None,
            point=SessionForkPoint(SessionForkMode.THROUGH_TURN, "turn-7"),
        )
    )

    assert result.mode is SessionForkMode.THROUGH_TURN
    assert result.through_turn_id == "turn-7"
    assert store.forked is not None
    assert store.forked.point.anchor_id == "turn-7"
    assert events[-2:] == ["fork", "publish"]
    assert publisher.published == [
        SessionForked(
            child_key="agent:main:webchat:child",
            parent_key="agent:main:webchat:parent",
        )
    ]


async def test_rename_owns_normalization_and_length_invariants() -> None:
    events: list[str] = []
    application = _application(events)

    result = await application.rename(
        RenameSession(" agent:main:webchat:session ", "  New title  ")
    )

    assert result.key == "agent:main:webchat:session"
    assert events[-1] == "rename:agent:main:webchat:session:New title"
    with pytest.raises(SessionDisplayNameError):
        await application.rename(RenameSession(result.key, "   "))
    with pytest.raises(SessionDisplayNameError):
        await application.rename(RenameSession(result.key, "x" * 513))


async def test_delete_reports_partial_failures_and_continues_in_order() -> None:
    events: list[str] = []
    deletion = _Deletion(events, failures={"agent:main:webchat:bad"})
    application = _application(events, deletion=deletion)

    result = await application.delete(
        DeleteSessions(
            (
                "agent:main:webchat:first",
                "agent:main:webchat:bad",
                "agent:main:webchat:last",
            )
        )
    )

    assert result.deleted == (
        "agent:main:webchat:first",
        "agent:main:webchat:last",
    )
    assert tuple(failure.requested_key for failure in result.failures) == (
        "agent:main:webchat:bad",
    )
    assert events[-3:] == [
        "delete:agent:main:webchat:first",
        "delete:agent:main:webchat:bad",
        "delete:agent:main:webchat:last",
    ]


class _FencedDeletion(_Deletion):
    def __init__(self, events: list[str], *, fail_after_release: bool) -> None:
        super().__init__(events)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.settled = False
        self.fail_after_release = fail_after_release

    async def delete_one(self, canonical_key: str) -> None:
        self.started.set()
        await self.release.wait()
        self.settled = True
        if self.fail_after_release:
            raise RuntimeError("failure after caller cancellation")


@pytest.mark.parametrize("fail_after_release", [False, True])
async def test_delete_settles_irreversible_operation_before_propagating_cancellation(
    fail_after_release: bool,
) -> None:
    events: list[str] = []
    deletion = _FencedDeletion(events, fail_after_release=fail_after_release)
    application = _application(events, deletion=deletion)
    task = asyncio.create_task(
        application.delete(DeleteSessions(("agent:main:webchat:session",)))
    )
    await deletion.started.wait()

    task.cancel()
    deletion.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert deletion.settled is True
