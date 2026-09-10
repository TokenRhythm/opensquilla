"""Native preparation preserves the pre-acceptance capability boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.application.turn_admission import AdmitTurn
from opensquilla.gateway import admission_preparation as preparation
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.run_mode import RunMode
from opensquilla.sandbox.run_context import RunContext
from opensquilla.session.models import SessionNode


def _route_dependencies(tmp_path: Path, *, guest: bool) -> dict:
    session = SessionNode(session_key="agent:main:synthetic", session_id="session-synthetic")
    return {
        "session": session,
        "key": session.session_key,
        "session_id": session.session_id,
        "atomic_intent_plan": None,
        "workspace_guard": None,
        "storage": SimpleNamespace(),
        "sessions": SimpleNamespace(update=AsyncMock()),
        "config": GatewayConfig(),
        "principal": Principal("operator", frozenset(), not guest, not guest),
        "conn_id": "connection-synthetic",
        "media_root": tmp_path,
        "preview_service": None,
        "effective_agent_id": lambda _session, _key: "main",
        "run_mode_hint": None,
        "elevated_hint": None,
        "guest_safe": guest,
        "guest_profile_factory": Mock(),
        "event_emitter_factory": Mock(return_value=AsyncMock()),
        "page_context_resolver": AsyncMock(),
    }


@pytest.mark.asyncio
async def test_unavailable_guest_sandbox_rejects_before_allocating_workspace(tmp_path, monkeypatch):
    capability = SimpleNamespace(available=False, to_payload=lambda: {"available": False})
    monkeypatch.setattr(
        preparation,
        "current_sandbox_capability_report",
        AsyncMock(return_value=capability),
    )
    deps = _route_dependencies(tmp_path, guest=True)
    with pytest.raises(RpcHandlerError) as caught:
        await preparation.prepare_route(AdmitTurn(deps["key"], "hello", "session"), **deps)
    assert caught.value.code == "SANDBOX_UNAVAILABLE"
    assert caught.value.details == {"reason": "sandbox_unavailable", "available": False}
    deps["guest_profile_factory"].assert_not_called()
    deps["sessions"].update.assert_not_awaited()




@pytest.mark.asyncio
async def test_page_context_is_user_content_on_the_normal_owner_route(tmp_path, monkeypatch):
    deps = _route_dependencies(tmp_path, guest=False)
    monkeypatch.setattr(
        preparation,
        "authoritative_project_run_context",
        AsyncMock(return_value=(RunContext(run_mode=RunMode.FULL, workspace=str(tmp_path)), None)),
    )
    monkeypatch.setattr(
        preparation, "resolve_default_run_mode",
        AsyncMock(return_value=(RunMode.FULL, "config")),
    )
    deps["page_context_resolver"].return_value = {
        "targetRef": "page-synthetic",
        "annotations": [{"text": "Use a blue heading"}],
    }
    command = AdmitTurn(
        deps["key"], "update the heading", "session",
        page_context={"targetRef": "page-synthetic"},
    )
    prepared = await preparation.prepare_route(command, **deps)
    deps["page_context_resolver"].assert_awaited_once_with(
        command.page_context, session_key=deps["key"], session_id=deps["session_id"],
        workspace=str(tmp_path),
    )
    assert "Use a blue heading" in prepared.page_context_text
    assert "artifact_context" not in prepared.envelope.runtime_services
    assert "turn_authority_cleanup" not in prepared.envelope.runtime_services
    assert prepared.host_execute_allowed is True
