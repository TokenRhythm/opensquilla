from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from opensquilla.application.admission_errors import AdmissionError, AdmissionUnavailableError
from opensquilla.application.admission_views import AdmissionStorageCapabilities
from opensquilla.application.turn_acceptance import DurableTurnAdmission
from opensquilla.application.turn_acceptance_ports import AdmissionPolicy
from opensquilla.application.turn_admission import AdmitTurn, PendingInputGuard
from opensquilla.run_mode import RunMode
from opensquilla.session.models import TurnIngressReceipt
from opensquilla.session.storage import TurnAcceptanceResult


@dataclass
class _NormalizedInput:
    message_text: str
    semantic_message: str
    generated_attachments: list = field(default_factory=list)
    metadata: dict = field(default_factory=lambda: {"guard_action": "none"})


class _ReplayStorage:
    capabilities = AdmissionStorageCapabilities(
        receipts=True, meta_controls=False, atomic_acceptance=False
    )

    def __init__(self, events: list[str], acceptance: TurnAcceptanceResult) -> None:
        self.events = events
        self.acceptance = acceptance
        self.consumed = []

    async def replay_turn_ingress_receipt(self, **identity):
        self.events.append("replay")
        assert identity == {
            "source_scope": "web:web:operator",
            "request_session_key": "agent:main:webchat:one",
            "client_request_id": "request-one",
        }
        return self.acceptance

    async def consume_replayed_pending_chat_input(self, **identity):
        self.events.append("consume-pending")
        self.consumed.append(identity)


class _ReplayPorts:
    is_owner = False
    policy = AdmissionPolicy(
        Path("unused-test-media"), True, None, 4096, False, RunMode.SAFE, RunMode.SAFE
    )

    def __init__(self, *, acceptance: TurnAcceptanceResult | None = None) -> None:
        self.events: list[str] = []
        self.sessions = object() if acceptance is not None else None
        self.storage = _ReplayStorage(self.events, acceptance) if acceptance is not None else None
        self.intent_entered = asyncio.Event()
        self.release_intent = asyncio.Event()
        self.release_intent.set()

    @asynccontextmanager
    async def explicit_ingress_intent(self, session_key):
        self.events.append("intent-enter")
        self.intent_entered.set()
        await self.release_intent.wait()
        try:
            yield
        finally:
            self.events.append("intent-exit")

    @asynccontextmanager
    async def authority_scope(self):
        self.events.append("authority-enter")
        try:
            yield
        finally:
            self.events.append("authority-exit")

    def clear_compaction_marker(self, session_key):
        self.events.append("clear-marker")

    def normalize_input(self, command):
        self.events.append("normalize")
        return _NormalizedInput(command.message, command.message)

    async def accepted_response(self, acceptance, **details):
        self.events.append("project")
        assert details["client_request_id"] == "request-one"
        return {
            "status": "accepted",
            "key": acceptance.receipt.accepted_session_key,
            "replayed": acceptance.replayed,
        }


def _command() -> AdmitTurn:
    return AdmitTurn(
        "agent:main:webchat:one",
        "synthetic message",
        "session",
        client_request_id="request-one",
        client_message_id="message-one",
        source_scope="web:web:operator",
        request_fingerprint="fingerprint-one",
    )


def _acceptance() -> TurnAcceptanceResult:
    return TurnAcceptanceResult(
        TurnIngressReceipt(
            source_scope="web:web:operator",
            request_session_key="agent:main:webchat:one",
            client_request_id="request-one",
            request_fingerprint="fingerprint-one",
            accepted_session_key="agent:main:webchat:one",
            session_id="session-one",
            message_id="message-one",
            task_id="turn-one",
        ),
        replayed=True,
        fresh_user_session=False,
    )


async def test_webchat_instant_accept_still_enters_intent_before_first_await() -> None:
    ports = _ReplayPorts()
    ports.release_intent.clear()
    task = asyncio.create_task(
        DurableTurnAdmission(ports).admit(replace(_command(), surface="webchat"))
    )
    await ports.intent_entered.wait()
    assert not task.done()
    assert ports.events == ["intent-enter"]
    ports.release_intent.set()

    assert await task == {
        "ok": True,
        "sessionKey": "agent:main:webchat:one",
        "instant_accept": True,
    }
    assert ports.events == ["intent-enter", "intent-exit"]


async def test_webchat_requires_durable_support_for_initial_controls() -> None:
    ports = _ReplayPorts()
    with pytest.raises(AdmissionUnavailableError, match="Initial session controls"):
        await DurableTurnAdmission(ports).admit(
            replace(_command(), surface="webchat", initial_routing_mode="router")
        )
    assert ports.events == ["intent-enter", "clear-marker", "intent-exit"]


async def test_receipt_replay_returns_before_artifact_material_or_runtime_preparation() -> None:
    ports = _ReplayPorts(acceptance=_acceptance())
    result = await DurableTurnAdmission(ports).admit(_command())
    assert result == {
        "status": "accepted",
        "key": "agent:main:webchat:one",
        "replayed": True,
    }
    assert ports.events == [
        "intent-enter",
        "authority-enter",
        "normalize",
        "replay",
        "project",
        "authority-exit",
        "intent-exit",
    ]


async def test_changed_replay_fingerprint_fails_before_projection_or_side_effects() -> None:
    ports = _ReplayPorts(acceptance=_acceptance())
    with pytest.raises(AdmissionError) as caught:
        await DurableTurnAdmission(ports).admit(
            replace(_command(), request_fingerprint="different")
        )
    assert caught.value.kind == "IDEMPOTENCY_CONFLICT"
    assert caught.value.accepted is False
    assert "project" not in ports.events
    assert ports.storage.consumed == []


async def test_pending_replay_validates_and_atomically_consumes_exact_revision() -> None:
    ports = _ReplayPorts(acceptance=_acceptance())
    command = replace(
        _command(),
        pending_input=PendingInputGuard("pending-one", "fingerprint-one", 3),
    )
    await DurableTurnAdmission(ports).admit(command)
    assert ports.storage.consumed == [
        {
            "pending_input_id": "pending-one",
            "session_key": command.session_key,
            "source_scope": command.source_scope,
            "client_request_id": "request-one",
            "client_message_id": "message-one",
            "request_fingerprint": "fingerprint-one",
            "expected_revision": 3,
        }
    ]
    assert ports.events.index("consume-pending") < ports.events.index("project")
