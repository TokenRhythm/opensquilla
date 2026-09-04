from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace

import pytest

from opensquilla.application.turn_admission import PendingInputGuard, SteerTurn
from opensquilla.application.turn_steering import (
    AppendedSteeringInput,
    NormalizedSteeringText,
    PreparedSteeringInput,
    RuntimeSteeringDecision,
    SteeringAcceptanceError,
    SteeringContext,
    SteeringDisposition,
    SteeringIdentity,
    SteeringIdentityConflictError,
    SteeringNotice,
    SteeringRollbackError,
    TurnSteering,
)
from opensquilla.project_workspaces import ProjectWorkspaceGuard
from opensquilla.session.models import SessionNode, TranscriptEntry, TurnIngressReceipt
from opensquilla.session.storage import TurnAcceptanceResult

KEY = "agent:main:webchat:steering-port"


def _command(**changes: object) -> SteerTurn:
    return replace(
        SteerTurn(
            session_key=KEY,
            message="change direction",
            mode="durable",
            expected_turn_id="turn-active",
            client_request_id="request-one",
            client_message_id="client-one",
            surface_id="surface-one",
            source_scope="web:web:operator:steer.v2",
            request_fingerprint="fingerprint-one",
        ),
        **changes,
    )


@dataclass
class _Ports:
    calls: list[str] = field(default_factory=list)
    accepted: TurnAcceptanceResult | None = None
    durable_available: bool = True
    legacy_available: bool = True
    current_turn: str | None = "turn-active"
    runtime_target: str = "turn-active"
    runtime_accepts: bool = True
    removed: bool = True
    persistence_error: Exception | None = None
    notify_error: Exception | None = None
    update_error: Exception | None = None
    disposition_error: Exception | None = None
    persisted_guard: PendingInputGuard | None = None
    persisted_epoch: int | None = None
    notices: list[SteeringNotice] = field(default_factory=list)
    entered_persist: asyncio.Event = field(default_factory=asyncio.Event)
    allow_persist: asyncio.Event = field(default_factory=asyncio.Event)
    pause_persist: bool = False
    acceptance_error: Exception | None = None

    def acceptance_failure(self, error: Exception) -> SteeringAcceptanceError | None:
        return SteeringAcceptanceError(error) if error is self.acceptance_error else None

    async def session(self, key: str) -> SessionNode:
        self.calls.append("session")
        return SessionNode(session_key=key, session_id="session-one", agent_id="main")

    def normalize(self, message: str, *, is_web_source: bool) -> NormalizedSteeringText:
        self.calls.append("normalize")
        return NormalizedSteeringText(message, message, False)

    async def receipt(self, identity: SteeringIdentity) -> TurnAcceptanceResult | None:
        self.calls.append("receipt")
        return replace(self.accepted, replayed=True) if self.accepted else None

    async def workspace_guard(self, session: SessionNode) -> ProjectWorkspaceGuard | None:
        self.calls.append("workspace")
        return None

    async def prepare(
        self,
        key: str,
        message: str,
        context: SteeringContext,
        session: SessionNode,
    ) -> PreparedSteeringInput:
        self.calls.append("prepare")
        return PreparedSteeringInput(
            TranscriptEntry(
                session_key=key,
                session_id=session.session_id,
                message_id="stored-message",
                role="user",
                content=message,
            ),
            7,
        )

    async def persist(
        self,
        prepared: PreparedSteeringInput,
        *,
        active_turn_id: str,
        identity: SteeringIdentity,
        workspace_guard: ProjectWorkspaceGuard | None,
        pending: PendingInputGuard | None,
    ) -> TurnAcceptanceResult:
        self.calls.append("persist")
        self.entered_persist.set()
        if self.pause_persist:
            await self.allow_persist.wait()
        if self.persistence_error is not None:
            raise self.persistence_error
        self.persisted_guard = pending
        self.persisted_epoch = prepared.expected_epoch
        self.accepted = TurnAcceptanceResult(
            TurnIngressReceipt(
                source_scope=identity.source_scope,
                request_session_key=identity.request_session_key,
                client_request_id=identity.client_request_id,
                request_fingerprint=identity.request_fingerprint,
                accepted_session_key=KEY,
                session_id="session-one",
                message_id=prepared.entry.message_id,
                task_id=active_turn_id,
            ),
            replayed=False,
            fresh_user_session=False,
        )
        return self.accepted

    async def admit_runtime(
        self,
        key: str,
        target: str,
        message: str,
        *,
        semantic_message: str,
        persist: Callable[[str], Awaitable[TurnAcceptanceResult]],
        client_request_id: str,
        client_message_id: str,
        surface_id: str,
    ) -> RuntimeSteeringDecision:
        self.calls.append("runtime-reserve")
        if not self.runtime_accepts:
            return RuntimeSteeringDecision(False, failure_code="NO_ACTIVE_TURN")
        accepted = await persist(self.runtime_target)
        self.calls.append("runtime-attach")
        return RuntimeSteeringDecision(True, self.runtime_target, accepted)

    def notify_appended(self, entry: TranscriptEntry) -> None:
        self.calls.append("notify")
        if self.notify_error:
            raise self.notify_error

    async def disposition(self, acceptance: TurnAcceptanceResult) -> SteeringDisposition:
        self.calls.append("disposition")
        if self.disposition_error:
            raise self.disposition_error
        return SteeringDisposition()

    async def active_turn(self, key: str) -> str | None:
        self.calls.append("active")
        return self.current_turn

    def session_lock(self, key: str) -> AbstractAsyncContextManager[object]:
        @asynccontextmanager
        async def lock():
            self.calls.append("lock")
            yield
            self.calls.append("unlock")

        return lock()

    async def append(
        self,
        key: str,
        message: str,
        context: SteeringContext,
    ) -> AppendedSteeringInput:
        self.calls.append("append")
        return AppendedSteeringInput(message, "stored-message")

    async def steer_runtime(
        self,
        key: str,
        message: str,
        *,
        semantic_message: str,
        message_id: str | None,
        client_message_id: str,
        surface_id: str,
    ) -> str | None:
        self.calls.append("legacy-attach")
        return self.runtime_target if self.runtime_accepts else None

    async def remove(self, key: str, message_id: str) -> bool:
        self.calls.append("remove")
        return self.removed

    async def update_context(self, key: str, message_id: str, context: SteeringContext) -> bool:
        self.calls.append(f"context-{context.disposition}")
        if self.update_error:
            raise self.update_error
        return True

    async def publish_steer(self, notice: SteeringNotice) -> None:
        self.calls.append("event-steer")
        self.notices.append(notice)

    async def publish_disposition(self, notice: SteeringNotice) -> None:
        self.calls.append("event-disposition")
        self.notices.append(notice)


async def test_durable_replay_precedes_preparation_and_consumes_pending_once() -> None:
    ports = _Ports()
    application = TurnSteering(ports)
    pending = PendingInputGuard("pending-one", "staged-fingerprint", 3, "web:web:operator")
    command = _command(pending_input=pending)
    first = await application.steer(command)
    assert first["accepted"] is True
    assert first["replayed"] is False
    assert ports.calls == [
        "session",
        "normalize",
        "receipt",
        "workspace",
        "prepare",
        "runtime-reserve",
        "persist",
        "runtime-attach",
        "notify",
        "event-steer",
        "event-disposition",
        "disposition",
    ]
    assert ports.persisted_epoch == 7
    assert ports.persisted_guard == pending
    assert ports.accepted.receipt.source_scope == "web:web:operator"
    assert ports.accepted.receipt.request_fingerprint == "staged-fingerprint"
    ports.calls.clear()
    replay = await application.steer(command)
    assert replay["replayed"] is True
    assert replay["user_message_id"] == first["user_message_id"]
    assert ports.calls == ["session", "normalize", "receipt", "disposition"]
    assert len(ports.notices) == 2


async def test_reused_identity_with_new_content_does_not_prepare_or_attach() -> None:
    ports = _Ports()
    application = TurnSteering(ports)
    await application.steer(_command())
    ports.calls.clear()
    with pytest.raises(SteeringIdentityConflictError):
        await application.steer(_command(request_fingerprint="different"))
    assert ports.calls == ["session", "normalize", "receipt"]


async def test_runtime_cannot_persist_for_a_different_expected_turn() -> None:
    ports = _Ports(runtime_target="replacement-turn")
    with pytest.raises(RuntimeError, match="changed the expected turn"):
        await TurnSteering(ports).steer(_command())
    assert "persist" not in ports.calls
    assert "runtime-attach" not in ports.calls
    assert ports.notices == []


async def test_failed_persistence_never_attaches_or_publishes() -> None:
    ports = _Ports(persistence_error=OSError("durable store failed"))
    with pytest.raises(OSError, match="durable store"):
        await TurnSteering(ports).steer(_command())
    assert "runtime-attach" not in ports.calls
    assert "notify" not in ports.calls
    assert ports.notices == []


async def test_acceptance_phase_uses_primitive_failure_projection_without_attaching() -> None:
    failure = RuntimeError("commit fencing failed")
    ports = _Ports(persistence_error=failure, acceptance_error=failure)
    with pytest.raises(SteeringAcceptanceError) as caught:
        await TurnSteering(ports).steer(_command())
    assert caught.value.failure is failure
    assert caught.value.__cause__ is failure
    assert "runtime-attach" not in ports.calls
    assert "notify" not in ports.calls
    assert ports.notices == []


async def test_disconnect_does_not_split_commit_from_runtime_attachment() -> None:
    ports = _Ports(pause_persist=True)
    pending = asyncio.create_task(TurnSteering(ports).steer(_command()))
    await ports.entered_persist.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    ports.allow_persist.set()
    accepted = await pending
    assert accepted["accepted"] is True
    assert ports.calls.count("persist") == 1
    assert ports.calls.count("runtime-attach") == 1
    assert len(ports.notices) == 2


@pytest.mark.parametrize("message,non_text", [("/compact", False), ("guide", True)])
async def test_unsupported_input_is_rejected_before_any_persistence(message, non_text) -> None:
    ports = _Ports()
    result = await TurnSteering(ports).steer(_command(message=message, has_non_text_input=non_text))
    assert result["accepted"] is False
    assert result["failure_code"] == "STEER_UNSUPPORTED_INPUT"
    assert result["fallback_safe"] is True
    assert ports.calls == []


async def test_legacy_rejected_append_rolls_back_before_safe_fallback() -> None:
    ports = _Ports(runtime_accepts=False)
    result = await TurnSteering(ports).steer(_command(mode="legacy"))
    assert result == {"status": "idle", "accepted": False, "key": KEY}
    assert ports.calls == [
        "session",
        "active",
        "normalize",
        "lock",
        "append",
        "unlock",
        "legacy-attach",
        "remove",
    ]
    assert ports.notices == []


async def test_dirty_legacy_rollback_never_reports_safe_fallback_even_if_context_fails() -> None:
    ports = _Ports(
        runtime_accepts=False, removed=False, update_error=OSError("context unavailable")
    )
    with pytest.raises(SteeringRollbackError) as caught:
        await TurnSteering(ports).steer(_command(mode="legacy"))
    assert caught.value.message_id == "stored-message"
    assert caught.value.target_turn_id == "turn-active"
    assert ports.calls[-3:] == ["remove", "context-rejected", "event-disposition"]
    assert ports.notices[0].rejected_orphan is True
    assert ports.notices[0].context.disposition == "rejected"
    assert ports.notices[0].context.revision == 2


async def test_legacy_acceptance_preserves_context_and_event_order() -> None:
    ports = _Ports()
    result = await TurnSteering(ports).steer(_command(mode="legacy"))
    assert result["accepted"] is True
    assert result["disposition"] == "next_safe_boundary"
    assert ports.calls[-3:] == ["context-steering", "event-steer", "event-disposition"]
    assert all(notice.context.disposition == "steering" for notice in ports.notices)
