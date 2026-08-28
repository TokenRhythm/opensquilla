from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.scopes import METHOD_SCOPES, WRITE_SCOPE
from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import (
    SessionStorage,
    WorkspaceNotFoundError,
    WorkspaceRemovedError,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

SESSION_KEY = "agent:main:webchat:move-target"

_DEFAULT_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset(["operator.admin"]),
    is_owner=True,
    authenticated=True,
)


def _make_ctx(session_manager=None) -> RpcContext:
    ctx = RpcContext(
        conn_id="test-conn",
        principal=_DEFAULT_PRINCIPAL,
        config=GatewayConfig(memory={"flush_enabled": False}),
    )
    ctx.session_manager = session_manager
    return ctx


class _FakeStorage:
    """Storage stand-in returning a canned outcome for bind_session_workspace_atomic."""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str | None]] = []

    async def bind_session_workspace_atomic(
        self, session_key: str, workspace_id: str | None
    ) -> bool:
        self.calls.append((session_key, workspace_id))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        assert isinstance(self.outcome, bool)
        return self.outcome


class _FakeManager:
    def __init__(self, storage: object) -> None:
        self._storage = storage


def _capture_emits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict]]:
    emitted: list[tuple[str, str, dict]] = []

    async def _record(
        _ctx: RpcContext,
        session_key: str,
        event_name: str,
        send_payload: dict,
    ) -> None:
        emitted.append((session_key, event_name, send_payload))

    monkeypatch.setattr(rpc_sessions, "_send_prepared_to_subscribers", _record)
    return emitted


def _expect_handler_error(
    excinfo: pytest.ExceptionInfo[BaseException],
    code: str,
    details: object = None,
) -> None:
    assert isinstance(excinfo.value, RpcHandlerError)
    assert excinfo.value.code == code
    if details is not None:
        assert excinfo.value.details == details


def test_scope_contract_write_scope() -> None:
    assert METHOD_SCOPES["sessions.moveToWorkspace"] == WRITE_SCOPE


@pytest.mark.asyncio
async def test_move_success_emits_after_commit_and_returns_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage(True)
    emitted = _capture_emits(monkeypatch)
    ctx = _make_ctx(_FakeManager(storage))

    result = await rpc_sessions._handle_sessions_move_to_workspace(
        {"key": SESSION_KEY, "workspaceId": "ws-1"}, ctx
    )

    assert result == {"key": SESSION_KEY, "workspaceId": "ws-1", "changed": True}
    assert storage.calls == [(SESSION_KEY, "ws-1")]
    assert emitted == [
        (
            SESSION_KEY,
            "sessions.changed",
            {
                "schema_version": 1,
                "key": SESSION_KEY,
                "reason": "moved",
                "workspaceId": "ws-1",
            },
        )
    ]


@pytest.mark.asyncio
async def test_move_unchanged_no_emit_returns_changed_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage(False)
    emitted = _capture_emits(monkeypatch)
    ctx = _make_ctx(_FakeManager(storage))

    result = await rpc_sessions._handle_sessions_move_to_workspace(
        {"key": SESSION_KEY, "workspaceId": "ws-1"}, ctx
    )

    assert result == {"key": SESSION_KEY, "workspaceId": "ws-1", "changed": False}
    assert emitted == []


@pytest.mark.asyncio
async def test_session_not_found_maps_to_session_not_found_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage(KeyError("Session not found: gone"))
    emitted = _capture_emits(monkeypatch)
    ctx = _make_ctx(_FakeManager(storage))

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sessions._handle_sessions_move_to_workspace(
            {"key": "agent:main:webchat:gone", "workspaceId": None}, ctx
        )

    _expect_handler_error(excinfo, "SESSION_NOT_FOUND")
    assert emitted == []


@pytest.mark.asyncio
async def test_workspace_not_found_maps_and_never_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage(WorkspaceNotFoundError("Project workspace not found: ws-x"))
    emitted = _capture_emits(monkeypatch)
    ctx = _make_ctx(_FakeManager(storage))

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sessions._handle_sessions_move_to_workspace(
            {"key": SESSION_KEY, "workspaceId": "ws-x"}, ctx
        )

    _expect_handler_error(excinfo, "WORKSPACE_NOT_FOUND", {"workspaceId": "ws-x"})
    assert emitted == []


@pytest.mark.asyncio
async def test_workspace_removed_fail_closed_and_never_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage(WorkspaceRemovedError("Project workspace has been removed"))
    emitted = _capture_emits(monkeypatch)
    ctx = _make_ctx(_FakeManager(storage))

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sessions._handle_sessions_move_to_workspace(
            {"key": SESSION_KEY, "workspaceId": "ws-removed"}, ctx
        )

    _expect_handler_error(excinfo, "WORKSPACE_REMOVED", {"workspaceId": "ws-removed"})
    assert emitted == []


@pytest.mark.asyncio
async def test_workspace_id_null_unbinds_and_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage(True)
    emitted = _capture_emits(monkeypatch)
    ctx = _make_ctx(_FakeManager(storage))

    result = await rpc_sessions._handle_sessions_move_to_workspace(
        {"key": SESSION_KEY, "workspaceId": None}, ctx
    )

    assert result == {"key": SESSION_KEY, "workspaceId": None, "changed": True}
    assert storage.calls == [(SESSION_KEY, None)]
    # build_sessions_changed_payload drops None state values, so an unbind
    # event carries no workspaceId field at all.
    assert emitted == [
        (
            SESSION_KEY,
            "sessions.changed",
            {
                "schema_version": 1,
                "key": SESSION_KEY,
                "reason": "moved",
            },
        )
    ]


@pytest.mark.asyncio
async def test_workspace_id_blank_string_rejected() -> None:
    storage = _FakeStorage(True)
    ctx = _make_ctx(_FakeManager(storage))

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sessions._handle_sessions_move_to_workspace(
            {"key": SESSION_KEY, "workspaceId": "   "}, ctx
        )

    _expect_handler_error(excinfo, "INVALID_PARAMS")
    assert storage.calls == []


@pytest.mark.asyncio
async def test_workspace_id_whitespace_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage(True)
    emitted = _capture_emits(monkeypatch)
    ctx = _make_ctx(_FakeManager(storage))

    result = await rpc_sessions._handle_sessions_move_to_workspace(
        {"key": SESSION_KEY, "workspaceId": "  ws-1  "}, ctx
    )

    assert storage.calls == [(SESSION_KEY, "ws-1")]
    assert result["workspaceId"] == "ws-1"
    assert emitted == [
        (
            SESSION_KEY,
            "sessions.changed",
            {
                "schema_version": 1,
                "key": SESSION_KEY,
                "reason": "moved",
                "workspaceId": "ws-1",
            },
        )
    ]


@pytest.mark.asyncio
async def test_missing_session_manager_raises_key_error() -> None:
    ctx = _make_ctx(None)
    with pytest.raises(KeyError):
        await rpc_sessions._handle_sessions_move_to_workspace(
            {"key": SESSION_KEY, "workspaceId": None}, ctx
        )


class _RealStorageManager:
    def __init__(self, storage: SessionStorage) -> None:
        self._storage = storage


@pytest.mark.asyncio
async def test_real_storage_end_to_end_bind_and_fail_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the handler with a real SessionStorage: happy path,
    idempotent repeat, removed-workspace fail-closed, and SESSION_NOT_FOUND."""
    db_path = str(tmp_path / "sessions.db")
    apply_pending(db_path, MIGRATIONS_DIR)
    storage = await SessionStorage.open(db_path)
    manager = _RealStorageManager(storage)
    ctx = _make_ctx(manager)
    emitted = _capture_emits(monkeypatch)

    try:
        workspace = await storage.create_or_restore_project_workspace(
            path=str(tmp_path / "project"),
            path_key=str(tmp_path / "project"),
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        session = SessionNode(
            session_key=SESSION_KEY,
            created_at=100,
            updated_at=100,
        )
        await storage.upsert_session(session)

        # Happy path: bind emits sessions.changed with the committed binding.
        result = await rpc_sessions._handle_sessions_move_to_workspace(
            {"key": SESSION_KEY, "workspaceId": workspace.workspace_id}, ctx
        )
        assert result["changed"] is True
        persisted = await storage.get_session(SESSION_KEY)
        assert persisted is not None
        assert persisted.workspace_id == workspace.workspace_id
        assert emitted == [
            (
                SESSION_KEY,
                "sessions.changed",
                {
                    "schema_version": 1,
                    "key": SESSION_KEY,
                    "reason": "moved",
                    "workspaceId": workspace.workspace_id,
                    "epoch": 0,
                },
            )
        ]

        # Idempotent repeat: no emit, changed False.
        result = await rpc_sessions._handle_sessions_move_to_workspace(
            {"key": SESSION_KEY, "workspaceId": workspace.workspace_id}, ctx
        )
        assert result["changed"] is False
        assert len(emitted) == 1

        # Remove the workspace, then a move to it must fail closed.
        await storage.remove_project_workspace(workspace.workspace_id)
        with pytest.raises(RpcHandlerError) as excinfo:
            await rpc_sessions._handle_sessions_move_to_workspace(
                {"key": SESSION_KEY, "workspaceId": workspace.workspace_id}, ctx
            )
        _expect_handler_error(excinfo, "WORKSPACE_REMOVED")

        # Unknown session maps to SESSION_NOT_FOUND.
        with pytest.raises(RpcHandlerError) as excinfo:
            await rpc_sessions._handle_sessions_move_to_workspace(
                {"key": "agent:main:webchat:missing", "workspaceId": None}, ctx
            )
        _expect_handler_error(excinfo, "SESSION_NOT_FOUND")
    finally:
        await storage.close()
