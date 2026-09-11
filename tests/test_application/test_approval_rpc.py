from __future__ import annotations

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.application.approval_rpc import (
    approval_extend_rpc_payload,
    approval_request_rpc_payload,
    approval_resolve_rpc_payload,
    approval_settings_rpc_payload,
    approval_snapshot_rpc_payload,
    approval_wait_decision_rpc_payload,
)


def test_approval_settings_rpc_payload_includes_node_inheritance() -> None:
    queue = ApprovalQueue(db_path=":memory:")
    try:
        settings = queue.set_settings(
            "prompt",
            allow_patterns=["uv *"],
            deny_patterns=["rm *"],
            node_id="node-1",
        )

        assert approval_settings_rpc_payload(
            settings,
            node_id="node-1",
            inherited=False,
        ) == {
            "mode": "prompt",
            "allowPatterns": ["uv *"],
            "denyPatterns": ["rm *"],
            "nodeId": "node-1",
            "inherited": False,
        }
    finally:
        queue.close()


def test_approval_request_rpc_payload_applies_settings_mode() -> None:
    queue = ApprovalQueue(db_path=":memory:")
    try:
        queue.set_settings("auto-approve")

        payload = approval_request_rpc_payload(
            queue,
            namespace="exec",
            params={"toolName": "exec_command", "args": {}, "sessionKey": "agent:main:demo"},
        )

        assert payload["mode"] == "auto-approve"
        assert payload["approved"] is True
        assert payload["resolved"] is True
        assert payload["pending"] is False
        assert queue.status(payload["id"])["params"]["approvalMode"] == "auto-approve"
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_wait_and_resolve_rpc_payloads_preserve_status_shape() -> None:
    queue = ApprovalQueue(db_path=":memory:", poll_interval=0.01)
    try:
        request = approval_request_rpc_payload(
            queue,
            namespace="plugin",
            params={"pluginId": "demo", "version": "1.0.0", "permissions": []},
        )
        approval_id = request["id"]

        resolved = approval_resolve_rpc_payload(queue, approval_id, True)
        waited = await approval_wait_decision_rpc_payload(queue, approval_id)

        assert resolved == waited
        assert isinstance(waited.pop("deadline"), float)
        assert waited == {
            "id": approval_id,
            "mode": "prompt",
            "approved": True,
            "resolved": True,
            "resolution": "approved",
            "consumed": False,
            "pending": False,
        }
    finally:
        queue.close()


def test_approval_extend_rpc_payload_pushes_deadline() -> None:
    queue = ApprovalQueue(db_path=":memory:", default_timeout=10.0)
    try:
        approval_id = queue.request(
            "exec",
            {"toolName": "exec_command", "command": "rm x", "sessionKey": "agent:main:demo"},
        )
        before = queue.get(approval_id).deadline

        payload = approval_extend_rpc_payload(queue, approval_id, 120.0)

        assert payload["pending"] is True
        assert payload["resolution"] == ""
        assert payload["deadline"] == before + 120.0
        assert queue.get(approval_id).deadline == before + 120.0
    finally:
        queue.close()


def test_approval_snapshot_payload_owns_wire_shape() -> None:
    queue = ApprovalQueue(db_path=":memory:")
    try:
        queue.set_settings("prompt")

        assert approval_snapshot_rpc_payload(queue) == {"mode": "prompt"}
    finally:
        queue.close()
