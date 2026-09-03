from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from opensquilla.application.turn_admission import (
    AdmitTurn,
    CancelTurn,
    SteerTurn,
    TurnAdmission,
)
from opensquilla.gateway import rpc_chat, rpc_sessions
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.rpc import RpcContext, get_dispatcher

ROOT = Path(__file__).resolve().parents[2]
TURN_METHODS = (
    "chat.send",
    "chat.abort",
    "sessions.send",
    "sessions.abort",
    "sessions.steer.v2",
    "sessions.steer",
)
COMPOSED_CONVERSATION_METHODS = (*TURN_METHODS, "chat.clarify_submit")


class _RecordingPorts:
    def __init__(self) -> None:
        self.commands: list[AdmitTurn | CancelTurn | SteerTurn] = []

    async def admit(self, command: AdmitTurn) -> dict[str, Any]:
        self.commands.append(command)
        return {"status": "accepted", "key": command.session_key}

    async def cancel(self, command: CancelTurn) -> dict[str, Any]:
        self.commands.append(command)
        return {"aborted": True, "key": command.session_key}

    async def steer(self, command: SteerTurn) -> dict[str, Any]:
        self.commands.append(command)
        return {"accepted": True, "key": command.session_key}


async def test_wire_surfaces_execute_one_shared_turn_application_once(
    monkeypatch,
) -> None:
    ports = _RecordingPorts()
    adapter = GatewayTurnAdmissionAdapter(
        TurnAdmission(
            ingress=ports,
            cancellation=ports,
            steering=ports,
        )
    )
    factory = lambda _context: adapter  # noqa: E731 - fixed test composition
    monkeypatch.setattr(rpc_chat, "_turn_admission_adapter_factory", factory)
    monkeypatch.setattr(rpc_sessions, "build_gateway_turn_admission_adapter", factory)

    dispatcher = get_dispatcher()
    context = RpcContext(conn_id="turn-admission-composition")
    cases = (
        (
            "chat.send",
            {"sessionKey": "agent:main:webchat:shared", "message": "hello"},
            AdmitTurn,
            "webchat",
        ),
        (
            "sessions.send",
            {"key": "agent:main:webchat:shared", "message": "hello"},
            AdmitTurn,
            "session",
        ),
        (
            "chat.abort",
            {"sessionKey": "agent:main:webchat:shared"},
            CancelTurn,
            "webchat",
        ),
        (
            "sessions.abort",
            {"key": "agent:main:webchat:shared"},
            CancelTurn,
            "session",
        ),
        (
            "sessions.steer.v2",
            {"key": "agent:main:webchat:shared", "message": "guide"},
            SteerTurn,
            "durable",
        ),
        (
            "sessions.steer",
            {"key": "agent:main:webchat:shared", "message": "guide"},
            SteerTurn,
            "legacy",
        ),
    )

    for index, (method, params, command_type, semantic) in enumerate(cases, start=1):
        response = await dispatcher.dispatch(str(index), method, params, context)

        assert response.ok is True
        assert len(ports.commands) == index
        command = ports.commands[-1]
        assert isinstance(command, command_type)
        if isinstance(command, (AdmitTurn, CancelTurn)):
            assert command.surface == semantic
        else:
            assert command.mode == semantic


def test_rpc_loader_is_the_only_fixed_turn_composition_boundary() -> None:
    loader = (ROOT / "src/opensquilla/gateway/rpc/__init__.py").read_text(encoding="utf-8")
    chat_import = loader.index("import opensquilla.gateway.rpc_chat as _rpc_chat")
    sessions_import = loader.index("import opensquilla.gateway.rpc_sessions as _rpc_sessions")
    composition = loader.index("_rpc_chat.bind_turn_admission_adapter_factory(")
    classification = loader.index("validate_classification()", composition)
    assert chat_import < sessions_import < composition < classification

    chat_path = ROOT / "src/opensquilla/gateway/rpc_chat.py"
    chat_tree = ast.parse(chat_path.read_text(encoding="utf-8"), filename=str(chat_path))
    assert [
        node
        for node in ast.walk(chat_tree)
        if isinstance(node, ast.ImportFrom) and node.module == "opensquilla.gateway.rpc_sessions"
    ] == []


def test_fresh_rpc_import_preserves_contract_entries_without_an_import_cycle() -> None:
    script = """
import opensquilla.gateway.rpc_chat as rpc_chat
import opensquilla.gateway.rpc_sessions as rpc_sessions
from opensquilla.gateway.rpc import get_dispatcher

methods = (
    "chat.send",
    "chat.abort",
    "sessions.send",
    "sessions.abort",
    "sessions.steer.v2",
    "sessions.steer",
    "chat.clarify_submit",
)
assert rpc_chat._turn_admission_adapter_factory is rpc_sessions.build_gateway_turn_admission_adapter
registry = get_dispatcher()
assert len(registry.list_methods()) == 306
for method in methods:
    entry = registry.get_entry(method)
    assert entry is not None
    assert entry.generated_contract_name == method
    assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
    assert entry.handler.__name__ == "handle_contract_method"
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )


def test_composed_conversation_method_fixture_is_exact() -> None:
    registry = get_dispatcher()
    assert tuple(
        method for method in registry.list_methods() if method in COMPOSED_CONVERSATION_METHODS
    ) == tuple(sorted(COMPOSED_CONVERSATION_METHODS))
