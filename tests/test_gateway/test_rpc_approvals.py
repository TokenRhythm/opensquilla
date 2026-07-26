from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.gateway import rpc_approvals
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import APPROVALS_SCOPE, METHOD_SCOPES


@pytest.mark.asyncio
async def test_exec_approval_rpc_delegates_payload_to_application_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = ApprovalQueue(db_path=":memory:")
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    try:
        queue.set_settings("auto-deny")

        result = await get_dispatcher().dispatch(
            "r1",
            "exec.approval.request",
            {"toolName": "exec_command", "args": {}, "sessionKey": "agent:main:demo"},
            RpcContext(conn_id="test"),
        )

        assert result.error is None, result.error
        assert result.payload["mode"] == "auto-deny"
        assert result.payload["approved"] is False
        assert result.payload["resolved"] is True
        assert result.payload["pending"] is False
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_exec_approval_extend_rpc_pushes_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = ApprovalQueue(db_path=":memory:", default_timeout=10.0)
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    try:
        approval_id = queue.request(
            "exec",
            {"toolName": "exec_command", "command": "rm x", "sessionKey": "agent:main:demo"},
        )
        before = queue.get(approval_id).deadline

        result = await get_dispatcher().dispatch(
            "r1",
            "exec.approval.extend",
            {"id": approval_id, "seconds": 120},
            RpcContext(conn_id="test"),
        )

        assert result.error is None, result.error
        assert result.payload["pending"] is True
        assert result.payload["deadline"] == before + 120
        assert queue.get(approval_id).deadline == before + 120
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_exec_approval_extend_rpc_rejects_non_positive_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = ApprovalQueue(db_path=":memory:", default_timeout=10.0)
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    try:
        approval_id = queue.request("exec", {"toolName": "exec_command", "command": "rm x"})

        result = await get_dispatcher().dispatch(
            "r1",
            "exec.approval.extend",
            {"id": approval_id, "seconds": 0},
            RpcContext(conn_id="test"),
        )

        assert result.error is not None
    finally:
        queue.close()


def _approval_ctx(*scopes: str) -> RpcContext:
    return RpcContext(
        conn_id="test",
        principal=Principal(
            role="operator",
            scopes=frozenset(scopes),
            is_owner=False,
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
async def test_approval_status_rpc_reports_pending_claimed_and_resolved_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = ApprovalQueue(db_path=":memory:")
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    ctx = _approval_ctx(APPROVALS_SCOPE)
    try:
        pending_id = queue.request("exec", {"toolName": "exec_command"})
        pending = await get_dispatcher().dispatch(
            "pending", "exec.approval.status", {"id": pending_id}, ctx
        )
        assert pending.error is None, pending.error
        assert pending.payload == {
            "found": True,
            "id": pending_id,
            "namespace": "exec",
            "pending": True,
            "resolutionInProgress": False,
            "resolved": False,
            "approved": False,
            "resolution": "",
            "consumed": False,
            "deadline": queue.get(pending_id).deadline,
        }

        claim_token = queue.claim_resolution(pending_id)
        claimed = await get_dispatcher().dispatch(
            "claimed", "exec.approval.status", {"id": pending_id}, ctx
        )
        assert claimed.error is None, claimed.error
        assert claimed.payload["pending"] is False
        assert claimed.payload["resolutionInProgress"] is True
        assert claimed.payload["resolved"] is False

        queue.finalize_claimed_resolution(pending_id, claim_token, True)
        finalizing = await get_dispatcher().dispatch(
            "finalizing", "exec.approval.status", {"id": pending_id}, ctx
        )
        assert finalizing.payload["resolutionInProgress"] is True
        assert finalizing.payload["resolved"] is False
        queue.complete_claimed_resolution(pending_id, claim_token)

        approved = await get_dispatcher().dispatch(
            "approved", "exec.approval.status", {"id": pending_id}, ctx
        )
        assert approved.error is None, approved.error
        assert approved.payload["pending"] is False
        assert approved.payload["resolutionInProgress"] is False
        assert approved.payload["resolved"] is True
        assert approved.payload["approved"] is True
        assert approved.payload["resolution"] == "approved"

        queue.consume(pending_id)
        consumed = await get_dispatcher().dispatch(
            "consumed", "exec.approval.status", {"id": pending_id}, ctx
        )
        assert consumed.payload["resolved"] is True
        assert consumed.payload["consumed"] is True
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_approval_status_rpc_reports_denied_expired_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = ApprovalQueue(db_path=":memory:")
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    ctx = _approval_ctx(APPROVALS_SCOPE)
    try:
        denied_id = queue.request("exec", {})
        queue.resolve(denied_id, False)
        denied = await get_dispatcher().dispatch(
            "denied", "exec.approval.status", {"id": denied_id}, ctx
        )
        assert denied.error is None, denied.error
        assert denied.payload["resolved"] is True
        assert denied.payload["approved"] is False
        assert denied.payload["resolution"] == "denied"

        expired_id = queue.request("exec", {})
        queue._rearm_deadline(expired_id, time.time() - 1)
        queue._expire_if_unresolved(expired_id)
        expired = await get_dispatcher().dispatch(
            "expired", "exec.approval.status", {"id": expired_id}, ctx
        )
        assert expired.error is None, expired.error
        assert expired.payload["resolved"] is True
        assert expired.payload["approved"] is False
        assert expired.payload["resolution"] == "expired"

        missing = await get_dispatcher().dispatch(
            "missing", "exec.approval.status", {"id": "not-present"}, ctx
        )
        assert missing.error is None, missing.error
        assert missing.payload == {
            "found": False,
            "id": "not-present",
            "namespace": "exec",
            "pending": False,
            "resolutionInProgress": False,
            "resolved": False,
        }
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_approval_status_rpc_enforces_namespace_and_approvals_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = ApprovalQueue(db_path=":memory:")
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    plugin_id = queue.request("plugin", {"pluginId": "demo", "permissions": []})
    try:
        plugin = await get_dispatcher().dispatch(
            "plugin",
            "plugin.approval.status",
            {"id": plugin_id},
            _approval_ctx(APPROVALS_SCOPE),
        )
        assert plugin.error is None, plugin.error
        assert plugin.payload["namespace"] == "plugin"

        wrong_namespace = await get_dispatcher().dispatch(
            "wrong",
            "exec.approval.status",
            {"id": plugin_id},
            _approval_ctx(APPROVALS_SCOPE),
        )
        assert wrong_namespace.error is not None
        assert wrong_namespace.error.code == "INVALID_REQUEST"

        unauthorized = await get_dispatcher().dispatch(
            "unauthorized",
            "plugin.approval.status",
            {"id": plugin_id},
            _approval_ctx("operator.read"),
        )
        assert unauthorized.error is not None
        assert unauthorized.error.code == "UNAUTHORIZED"
        assert METHOD_SCOPES["exec.approval.status"] == APPROVALS_SCOPE
        assert METHOD_SCOPES["plugin.approval.status"] == APPROVALS_SCOPE
    finally:
        queue.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("first_decision", "stale_decision"), [(True, False), (False, True)])
async def test_plugin_approval_resolve_returns_first_cross_surface_decision(
    monkeypatch: pytest.MonkeyPatch,
    first_decision: bool,
    stale_decision: bool,
) -> None:
    queue = ApprovalQueue(db_path=":memory:")
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    try:
        approval_id = queue.request(
            "plugin",
            {"pluginId": "demo", "version": "1.0.0", "permissions": ["read"]},
        )
        ctx = RpcContext(conn_id="test")

        first = await get_dispatcher().dispatch(
            "r1",
            "plugin.approval.resolve",
            {"id": approval_id, "approved": first_decision},
            ctx,
        )
        stale = await get_dispatcher().dispatch(
            "r2",
            "plugin.approval.resolve",
            {"id": approval_id, "approved": stale_decision},
            ctx,
        )

        assert first.error is None, first.error
        assert stale.error is None, stale.error
        assert first.payload["approved"] is first_decision
        assert stale.payload["approved"] is first_decision
        assert stale.payload["resolved"] is True
        assert stale.payload["pending"] is False
        assert queue.get(approval_id).approved is first_decision
    finally:
        queue.close()


def test_gateway_rpc_approvals_keeps_payload_logic_out_of_gateway_boundary() -> None:
    source = Path(rpc_approvals.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]

    top_level_functions = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helper_names = {
        "approval_forget_rpc_payload",
        "approval_snapshot_rpc_payload",
    }
    imported_helpers = {
        alias.name
        for node in imports
        if node.module == "opensquilla.application.approval_rpc"
        for alias in node.names
    }
    handlers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {"_handle_exec_approval_snapshot", "_handle_exec_approval_forget"}
    }
    handler_names = {
        node.id
        for handler in handlers.values()
        for node in ast.walk(handler)
        if isinstance(node, ast.Name)
    }
    direct_key_sets = {
        tuple(key.value for key in node.keys if isinstance(key, ast.Constant))
        for handler in handlers.values()
        for node in ast.walk(handler)
        if isinstance(node, ast.Dict)
    }
    private_attrs = {
        node.attr
        for handler in handlers.values()
        for node in ast.walk(handler)
        if isinstance(node, ast.Attribute)
    }

    assert "_settings_payload" not in top_level_functions
    assert "_status_payload" not in top_level_functions
    assert "_request_approval" not in top_level_functions
    assert helper_names.issubset(imported_helpers)
    assert helper_names.issubset(handler_names)
    assert ("mode", "intent_cache_size", "intent_cache_entries") not in direct_key_sets
    assert ("kind", "target", "scope") not in direct_key_sets
    assert ("scope", "target") not in direct_key_sets
    assert ("scope",) not in direct_key_sets
    assert "_entries" not in private_attrs
