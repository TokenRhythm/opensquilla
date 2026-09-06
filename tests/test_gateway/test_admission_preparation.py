"""Native preparation preserves the pre-acceptance capability boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.application.turn_admission import AdmitTurn
from opensquilla.application.turn_input import DocumentTurnContext
from opensquilla.gateway import admission_preparation as preparation
from opensquilla.gateway.artifact_contexts import BoundPromptAnnotationContext
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.run_mode import RunMode
from opensquilla.sandbox.run_context import RunContext
from opensquilla.session.models import SessionNode


@pytest.mark.asyncio
async def test_plain_turn_does_not_open_artifact_services(tmp_path: Path, monkeypatch) -> None:
    open_service = AsyncMock()
    monkeypatch.setattr(preparation.ArtifactSessionService, "from_session_storage", open_service)
    focus = AsyncMock()
    emitter = Mock()
    binding = await preparation.bind_artifact(
        AdmitTurn("agent:main:synthetic", "hello", "session"),
        key="agent:main:synthetic",
        session_id="session-synthetic",
        session=SessionNode(session_key="agent:main:synthetic", session_id="session-synthetic"),
        storage=SimpleNamespace(),
        media_root=tmp_path,
        principal_actor_id=None,
        event_emitter_factory=emitter,
        load_followup_focus=focus,
    )
    assert binding == preparation.ArtifactBinding()
    open_service.assert_not_awaited()
    focus.assert_not_awaited()
    emitter.assert_not_called()


@pytest.mark.asyncio
async def test_document_scope_mismatch_fails_before_focus_or_events(tmp_path: Path, monkeypatch):
    document = SimpleNamespace(
        session_key="agent:main:another",
        session_id="session-synthetic",
        document_id="document-synthetic",
        head_revision_id="revision-synthetic",
    )
    revision = SimpleNamespace(
        document_id="document-synthetic",
        revision_id="revision-synthetic",
    )
    service = SimpleNamespace(
        get_document_head=AsyncMock(
            return_value=SimpleNamespace(document=document, revision=revision),
        )
    )
    monkeypatch.setattr(
        preparation.ArtifactSessionService,
        "from_session_storage",
        AsyncMock(return_value=service),
    )
    focus, emitter = AsyncMock(), Mock()
    with pytest.raises(RpcHandlerError) as caught:
        await preparation.bind_artifact(
            AdmitTurn(
                "agent:main:synthetic",
                "edit",
                "session",
                document_context=DocumentTurnContext("document-synthetic", "revision-synthetic"),
            ),
            key="agent:main:synthetic",
            session_id="session-synthetic",
            session=SessionNode(session_key="agent:main:synthetic", session_id="session-synthetic"),
            storage=SimpleNamespace(),
            media_root=tmp_path,
            principal_actor_id=None,
            event_emitter_factory=emitter,
            load_followup_focus=focus,
        )
    assert caught.value.code == "DOCUMENT_UNAVAILABLE"
    assert caught.value.retryable is False
    focus.assert_not_awaited()
    emitter.assert_not_called()


def _route_dependencies(tmp_path: Path, *, guest: bool) -> dict:
    session = SessionNode(session_key="agent:main:synthetic", session_id="session-synthetic")
    return {
        "session": session,
        "key": session.session_key,
        "session_id": session.session_id,
        "atomic_intent_plan": None,
        "binding": preparation.ArtifactBinding(),
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
        "candidate_loop_supported": lambda _capabilities: False,
        "source_only_context": lambda context: replace(
            context, tool_names=frozenset({"document_read"})
        ),
        "authority_scope": None,
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
async def test_incomplete_desktop_bridge_releases_authority_and_updates_binding(
    tmp_path, monkeypatch
):
    from opensquilla.gateway import desktop_artifact_bridge

    deps = _route_dependencies(tmp_path, guest=False)
    context = BoundPromptAnnotationContext(
        session_key=deps["key"],
        session_id=deps["session_id"],
        document_id="document-synthetic",
        revision_id="revision-synthetic",
        snapshots=(),
        artifact_format="html",
        tool_names=frozenset({"document_read", "document_finish"}),
        operation_class="selection_edit",
        request_context_prompt="synthetic context",
    )
    deps["binding"] = preparation.ArtifactBinding(context=context, service=SimpleNamespace())
    order: list[str] = []

    async def capabilities():
        order.append("capabilities")
        raise RuntimeError("synthetic capability failure")

    async def close():
        order.append("close")

    lease = SimpleNamespace(capabilities=capabilities, aclose=close)
    bridge = SimpleNamespace(acquire_binding=AsyncMock(return_value=lease))
    deps["authority_scope"] = SimpleNamespace(register=lambda _cleanup: order.append("register"))
    monkeypatch.setattr(
        desktop_artifact_bridge, "get_desktop_artifact_bridge_client", lambda: bridge
    )
    monkeypatch.setattr(
        preparation,
        "authoritative_project_run_context",
        AsyncMock(return_value=(RunContext(run_mode=RunMode.FULL), None)),
    )
    monkeypatch.setattr(
        preparation,
        "resolve_default_run_mode",
        AsyncMock(return_value=(RunMode.FULL, "config")),
    )
    prepared = await preparation.prepare_route(AdmitTurn(deps["key"], "edit", "session"), **deps)
    assert order == ["register", "capabilities", "close"]
    assert deps["binding"].context.tool_names == frozenset({"document_read"})
    assert prepared.envelope.runtime_services["artifact_context"] is deps["binding"].context
    assert "desktop_artifact_bridge" not in prepared.envelope.runtime_services
    assert "turn_cleanup_callbacks" not in prepared.envelope.runtime_services
    assert prepared.host_execute_allowed is True
    deps["sessions"].update.assert_not_awaited()
