from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from opensquilla.gateway import rpc_sandbox  # noqa: F401 — register sandbox RPCs
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.run_mode import RunMode
from opensquilla.sandbox.escalation import (
    remember_resolved_run_context,
    reset_resolved_run_context_overlays,
    resolved_run_context_overlay,
)
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    RunContext,
    get_run_context,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage


@pytest_asyncio.fixture(params=["managed", "configured"])
async def execution_workspace_ctx(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[RpcContext, SessionNode, Path]]:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    original = tmp_path / "original"
    original.mkdir()
    config = GatewayConfig(
        **({"workspace_dir": str(original)} if request.param == "configured" else {}),
        memory={"flush_enabled": False},
    )
    factory = build_execution_workspace_factory(config, profile_home=tmp_path / "profile")
    reset_resolved_run_context_overlays()
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(
            storage,
            execution_workspace_factory=factory if request.param is not None else None,
        )
        session = await manager.create("agent:main:webchat:workspace-set")
        if request.param is not None:
            assert session.execution_workspace["kind"] == request.param
            original = Path(session.execution_workspace["root"])
        session = await manager.update(
            session.session_key,
            origin={
                "preserved": "session metadata",
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "safe",
                    "workspace": str(original),
                },
            },
        )
        ctx = RpcContext(
            conn_id="workspace-set",
            principal=Principal(
                role="operator",
                scopes=frozenset({"operator.read", "operator.write"}),
                is_owner=True,
                authenticated=True,
            ),
            session_manager=manager,
            config=config,
        )
        try:
            yield ctx, session, original
        finally:
            reset_resolved_run_context_overlays()


@pytest.mark.asyncio
async def test_bound_execution_workspace_rejects_change_without_mutation(
    execution_workspace_ctx: tuple[RpcContext, SessionNode, Path],
    tmp_path: Path,
) -> None:
    ctx, session, original = execution_workspace_ctx
    selected = tmp_path / "selected"
    selected.mkdir()
    overlay = RunContext(run_mode=RunMode.FULL, workspace=str(original))
    remember_resolved_run_context(session.session_key, str(original), overlay)
    storage = ctx.session_manager.storage
    changes_before = storage.conn.total_changes

    response = await get_dispatcher().dispatch(
        "set", "sandbox.workspace.set",
        {"sessionKey": session.session_key, "workspace": str(selected)}, ctx,
    )

    assert response.error is not None
    assert response.error.code == "EXECUTION_WORKSPACE_FIXED"
    persisted = await storage.get_session(session.session_key)
    assert persisted.model_dump() == session.model_dump()
    assert storage.conn.total_changes == changes_before
    assert resolved_run_context_overlay(session.session_key, str(original)) is overlay
    assert resolved_run_context_overlay(session.session_key, str(selected)) is None
    following = await get_run_context(
        ctx.session_manager, session.session_key, config=ctx.config,
        workspace=ctx.config.workspace_dir, include_user_grants=False,
    )
    assert following.workspace == str(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("path_form", ["exact", "parent", "symlink"])
async def test_bound_execution_workspace_same_canonical_root_is_idempotent(
    execution_workspace_ctx: tuple[RpcContext, SessionNode, Path],
    tmp_path: Path,
    path_form: str,
) -> None:
    ctx, session, original = execution_workspace_ctx
    selected = original
    if path_form == "parent":
        child = original / "child"
        child.mkdir()
        selected = child / ".."
    elif path_form == "symlink":
        selected = tmp_path / "alias"
        selected.symlink_to(original, target_is_directory=True)
    overlay = RunContext(run_mode=RunMode.FULL, workspace=str(original))
    remember_resolved_run_context(session.session_key, str(original), overlay)
    storage = ctx.session_manager.storage
    changes_before = storage.conn.total_changes

    response = await get_dispatcher().dispatch(
        "set", "sandbox.workspace.set",
        {"sessionKey": session.session_key, "workspacePath": str(selected)}, ctx,
    )

    assert response.error is None
    assert response.payload["workspace"] == str(original)
    assert response.payload["runMode"] == "safe"
    persisted = await storage.get_session(session.session_key)
    assert persisted.model_dump() == session.model_dump()
    assert storage.conn.total_changes == changes_before
    assert resolved_run_context_overlay(session.session_key, str(original)) is overlay


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_binding", "reason"),
    [("empty", "binding_changed"), ("no_root", "binding_changed"),
     ("missing_directory", "unavailable"), ("symlink_root", "unavailable")],
)
async def test_bound_execution_workspace_set_preserves_binding_validation(
    execution_workspace_ctx: tuple[RpcContext, SessionNode, Path],
    tmp_path: Path,
    invalid_binding: str,
    reason: str,
) -> None:
    ctx, session, original = execution_workspace_ctx
    storage = ctx.session_manager.storage
    if invalid_binding in {"empty", "no_root"}:
        session.execution_workspace = (
            {} if invalid_binding == "empty"
            else {key: value for key, value in session.execution_workspace.items() if key != "root"}
        )
        await storage.upsert_session(session)
    else:
        original.rmdir()
        if invalid_binding == "symlink_root":
            replacement = tmp_path / "replacement"
            replacement.mkdir()
            original.symlink_to(replacement, target_is_directory=True)
    changes_before = storage.conn.total_changes

    response = await get_dispatcher().dispatch(
        "set", "sandbox.workspace.set",
        {"sessionKey": session.session_key, "workspace": str(original)}, ctx,
    )

    assert response.error is not None
    assert response.error.code == "WORKSPACE_UNAVAILABLE"
    assert response.error.details == {"reason": reason}
    persisted = await storage.get_session(session.session_key)
    assert persisted.model_dump() == session.model_dump()
    assert storage.conn.total_changes == changes_before
    assert resolved_run_context_overlay(session.session_key, str(original)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_workspace_ctx", [None], indirect=True)
async def test_legacy_workspace_set_still_persists_selected_root(
    execution_workspace_ctx: tuple[RpcContext, SessionNode, Path],
    tmp_path: Path,
) -> None:
    ctx, session, _original = execution_workspace_ctx
    selected = tmp_path / "selected"
    selected.mkdir()

    response = await get_dispatcher().dispatch(
        "set", "sandbox.workspace.set",
        {"sessionKey": session.session_key, "workspace": str(selected)}, ctx,
    )

    assert response.error is None
    persisted = await ctx.session_manager.storage.get_session(session.session_key)
    assert persisted.execution_workspace is None
    assert persisted.origin[RUN_CONTEXT_ORIGIN_KEY]["workspace"] == str(selected)
    following = await get_run_context(
        ctx.session_manager, session.session_key, config=ctx.config,
        workspace=ctx.config.workspace_dir, include_user_grants=False,
    )
    assert response.payload["workspace"] == following.workspace == str(selected)
