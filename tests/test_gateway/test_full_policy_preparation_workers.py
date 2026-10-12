from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from opensquilla.application.turn_admission import AdmitTurn
from opensquilla.gateway import admission_preparation, native_attachments
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.run_mode import RunMode
from opensquilla.sandbox import policy_store
from opensquilla.sandbox.run_context import RunContext
from opensquilla.session.models import SessionNode


def _observe_store(monkeypatch):
    seen = []
    original = policy_store.SandboxPolicyStore

    class ObservedStore(original):
        def __init__(self, path):
            seen.append(("constructor", threading.get_ident()))
            super().__init__(path)

        def read(self):
            seen.append(("read", threading.get_ident()))
            return super().read()

    monkeypatch.setattr(policy_store, "SandboxPolicyStore", ObservedStore)
    return seen


async def test_full_native_selection_pins_policy_off_loop(tmp_path, monkeypatch):
    from opensquilla.agents import scope
    from opensquilla.gateway import project_workspace_runtime

    session = SessionNode(session_key="agent:main:worker", session_id="native-worker")
    manager = SimpleNamespace(storage=object(), get_session=AsyncMock(return_value=session))
    monkeypatch.setattr(scope, "resolve_agent_workspace_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(
        project_workspace_runtime, "authoritative_project_run_context",
        AsyncMock(return_value=(RunContext(run_mode=RunMode.FULL, workspace=str(tmp_path)), None)),
    )
    seen = _observe_store(monkeypatch)
    loop_thread = threading.get_ident()
    _, _, context = await native_attachments.native_selection_context(
        GatewayConfig(state_dir=str(tmp_path)), manager, session.session_key,
    )
    assert context.run_mode == "full"
    assert context.sandbox_policy.policy_version == 0
    assert [kind for kind, _thread in seen] == ["constructor", "read"]
    assert all(thread != loop_thread for _kind, thread in seen)


async def test_full_workspace_file_admission_pins_policy_off_loop(tmp_path, monkeypatch):
    from opensquilla import workspace_files

    session = SessionNode(session_key="agent:main:worker", session_id="admission-worker")
    monkeypatch.setattr(
        admission_preparation, "authoritative_project_run_context",
        AsyncMock(return_value=(RunContext(run_mode=RunMode.FULL, workspace=str(tmp_path)), None)),
    )
    monkeypatch.setattr(
        admission_preparation, "resolve_default_run_mode",
        AsyncMock(return_value=(RunMode.FULL, "config")),
    )
    validate = AsyncMock()
    monkeypatch.setattr(workspace_files, "validate_workspace_files", validate)
    seen = _observe_store(monkeypatch)
    loop_thread = threading.get_ident()
    command = AdmitTurn(
        session.session_key, "read selected file", "session",
        workspace_files=({"path": "selected.txt"},),
    )
    prepared = await admission_preparation.prepare_route(
        command, session=session, key=session.session_key, session_id=session.session_id,
        atomic_intent_plan=None, workspace_guard=None, storage=SimpleNamespace(),
        sessions=SimpleNamespace(update=AsyncMock()), config=GatewayConfig(state_dir=str(tmp_path)),
        principal=Principal("operator", frozenset(), True, True), conn_id="policy-worker",
        media_root=tmp_path, preview_service=None, effective_agent_id=lambda *_args: "main",
        run_mode_hint=None, elevated_hint=None, guest_safe=False, guest_profile_factory=Mock(),
        event_emitter_factory=Mock(return_value=AsyncMock()), page_context_resolver=AsyncMock(),
    )
    assert prepared.host_execute_allowed
    validate.assert_awaited_once()
    context = validate.call_args.kwargs["tool_context"]
    assert context.run_mode == "full"
    assert context.sandbox_policy.policy_version == 0
    assert [kind for kind, _thread in seen] == ["constructor", "read"]
    assert all(thread != loop_thread for _kind, thread in seen)
