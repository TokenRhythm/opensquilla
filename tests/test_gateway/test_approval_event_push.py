"""Approval queue transitions push WS events to approvals-scoped clients."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

import opensquilla.gateway.app as app_module
from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.gateway import rpc_approvals
from opensquilla.gateway.approval_events import (
    approval_event_name,
    build_approval_event_payload,
    build_approval_snapshot_item,
    register_approval_event_bridge,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.event_bridge import EventBridge


class _FakeConn:
    def __init__(self, conn_id: str, scopes: frozenset[str], role: str = "operator") -> None:
        self.conn_id = conn_id
        self.principal = Principal(
            role=role,
            scopes=scopes,
            is_owner=False,
            authenticated=True,
        )
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def send_event(self, event: str, payload: Any = None) -> None:
        self.events.append((event, payload))


class _FakeRegistry:
    def __init__(self, conns: list[_FakeConn]) -> None:
        self._conns = {conn.conn_id: conn for conn in conns}

    def all(self) -> list[_FakeConn]:
        return list(self._conns.values())

    def get(self, conn_id: str) -> _FakeConn | None:
        return self._conns.get(conn_id)


def _build_bridge(
    conns: list[_FakeConn],
) -> tuple[ApprovalQueue, list[Any], Any]:
    queue = ApprovalQueue(db_path=":memory:")
    bridge = EventBridge(
        subscription_manager=None,
        connection_registry=_FakeRegistry(conns),
    )
    scheduled: list[Any] = []
    remove = register_approval_event_bridge(queue, bridge, schedule=scheduled.append)
    return queue, scheduled, remove


@pytest.mark.asyncio
async def test_exec_approval_request_pushes_event_to_approvals_scoped_client() -> None:
    approvals_conn = _FakeConn("c-approvals", frozenset({"operator.approvals"}))
    read_only_conn = _FakeConn("c-read", frozenset({"operator.read"}))
    node_conn = _FakeConn("c-node", frozenset({"node"}), role="node")
    queue, scheduled, remove = _build_bridge([approvals_conn, read_only_conn, node_conn])
    try:
        approval_id = queue.request(
            namespace="exec",
            params={
                "toolName": "exec_command",
                "command": "rm -rf ./scratch dir",
                "args": {"command": "rm -rf ./scratch dir", "workdir": None},
                "sessionKey": "agent:main:webchat:demo",
                "agent": "main",
            },
        )

        assert len(scheduled) == 1
        await scheduled.pop()

        assert len(approvals_conn.events) == 1
        event_name, payload = approvals_conn.events[0]
        assert event_name == "exec.approval.requested"
        assert payload["approval_id"] == approval_id
        assert payload["namespace"] == "exec"
        assert payload["session_key"] == "agent:main:webchat:demo"
        assert payload["tool_name"] == "exec_command"
        assert payload["command"] == "rm -rf ./scratch dir"
        assert payload["agent"] == "main"
        assert payload["created_at"] > 0
        assert payload["deadline"] > 0
        assert payload["args"] == {"command": "rm -rf ./scratch dir", "workdir": None}
        assert payload["warning"] == ""
        assert "approved" not in payload
        # The approvals surface stays scoped: read-only and node-role
        # connections must not receive approval pushes.
        assert read_only_conn.events == []
        assert node_conn.events == []
    finally:
        remove()
        queue.close()


@pytest.mark.asyncio
async def test_exec_approval_resolution_mirrors_resolved_event() -> None:
    approvals_conn = _FakeConn("c-approvals", frozenset({"operator.approvals"}))
    queue, scheduled, remove = _build_bridge([approvals_conn])
    try:
        approval_id = queue.request(
            namespace="exec",
            params={
                "toolName": "exec_command",
                "command": "echo hi",
                "sessionKey": "agent:main:webchat:demo",
            },
        )
        await scheduled.pop()

        queue.resolve(approval_id, True)

        assert len(scheduled) == 1
        await scheduled.pop()
        event_name, payload = approvals_conn.events[-1]
        assert event_name == "exec.approval.resolved"
        assert payload["approval_id"] == approval_id
        assert payload["session_key"] == "agent:main:webchat:demo"
        assert payload["approved"] is True
        assert payload["resolution"] == "approved"

        # Idempotent re-resolution must not emit a second resolved event.
        queue.resolve(approval_id, True)
        assert scheduled == []
    finally:
        remove()
        queue.close()


@pytest.mark.asyncio
async def test_auto_review_promotion_pushes_one_human_approval_request() -> None:
    approvals_conn = _FakeConn("c-approvals", frozenset({"operator.approvals"}))
    queue, scheduled, remove = _build_bridge([approvals_conn])
    try:
        approval_id = queue.request(
            namespace="exec",
            params={
                "toolName": "exec_command",
                "command": "critical operation",
                "sessionKey": "agent:main:webchat:demo",
                "reviewer": "auto_review",
                "humanActionable": False,
            },
        )
        assert scheduled == []

        queue.update_params(
            approval_id,
            {
                "toolName": "exec_command",
                "command": "critical operation",
                "sessionKey": "agent:main:webchat:demo",
                "reviewer": "user",
                "humanActionable": True,
            },
        )

        assert len(scheduled) == 1
        await scheduled.pop()
        assert [name for name, _payload in approvals_conn.events] == ["exec.approval.requested"]
        assert approvals_conn.events[0][1]["approval_id"] == approval_id

        queue.update_params(
            approval_id,
            {
                "toolName": "exec_command",
                "command": "critical operation",
                "sessionKey": "agent:main:webchat:demo",
                "reviewer": "user",
                "humanActionable": True,
                "reviewStatus": "still_waiting",
            },
        )
        assert scheduled == []
    finally:
        remove()
        queue.close()


@pytest.mark.asyncio
async def test_plugin_approval_events_use_plugin_namespace() -> None:
    approvals_conn = _FakeConn("c-approvals", frozenset({"operator.approvals"}))
    queue, scheduled, remove = _build_bridge([approvals_conn])
    try:
        approval_id = queue.request(
            namespace="plugin",
            params={"pluginId": "demo-plugin", "version": "1.0.0", "permissions": []},
        )
        await scheduled.pop()
        queue.resolve(approval_id, False)
        await scheduled.pop()

        assert [name for name, _ in approvals_conn.events] == [
            "plugin.approval.requested",
            "plugin.approval.resolved",
        ]
        requested_payload = approvals_conn.events[0][1]
        assert requested_payload["tool_name"] == "demo-plugin"
        resolved_payload = approvals_conn.events[1][1]
        assert resolved_payload["approved"] is False
        assert resolved_payload["resolution"] == "denied"
    finally:
        remove()
        queue.close()


def test_build_approval_event_payload_falls_back_to_argv_command() -> None:
    payload = build_approval_event_payload(
        {
            "id": "abc123",
            "namespace": "exec",
            "params": {"argv": ["git", "status"], "action_kind": "exec"},
            "created_at": 1.0,
            "deadline": 2.0,
            "resolved": False,
            "approved": False,
        }
    )

    assert payload["command"] == "git status"
    assert payload["tool_name"] == "exec"
    assert payload["session_key"] == ""
    assert payload["deadline"] == 2.0


def test_build_approval_event_payload_includes_sandbox_kind() -> None:
    payload = build_approval_event_payload(
        {
            "id": "sandbox123",
            "namespace": "exec",
            "params": {
                "approvalKind": "sandbox_network",
                "sessionKey": "agent:main:webchat:demo",
            },
            "created_at": 1.0,
            "deadline": 2.0,
            "resolved": False,
            "approved": False,
        }
    )

    assert payload["approval_kind"] == "sandbox_network"
    assert payload["tool_name"] == "sandbox_network"
    assert payload["args"] is None
    assert payload["warning"] == ""


def test_sandbox_display_projection_whitelists_context_and_drops_policy_internals() -> None:
    info = {
        "id": "sandbox-network-123",
        "namespace": "exec",
        "params": {
            "approvalKind": "sandbox_network",
            "host": "packages.example.test",
            "bundle_id": "python-build",
            "workspace": "/workspace/project",
            "fingerprint": "secret-fingerprint",
            "reviewFingerprint": "secret-review-fingerprint",
            "reviewer": "user",
            "action": {"authorization": "Bearer secret-token"},
            "choices": [{"id": "allow_once"}],
            "sessionKey": "agent:main:webchat:demo",
        },
        "created_at": 1.0,
        "deadline": 2.0,
        "resolved": False,
        "approved": False,
    }

    push = build_approval_event_payload(info)
    snapshot = build_approval_snapshot_item(info, default_mode="prompt")

    expected_args = {
        "host": "packages.example.test",
        "bundle_id": "python-build",
        "workspace": "/workspace/project",
    }
    assert push["args"] == expected_args
    assert snapshot["args"] == expected_args
    assert snapshot["approvalKind"] == "sandbox_network"
    assert "params" not in snapshot
    encoded = json.dumps({"push": push, "snapshot": snapshot})
    assert "secret-fingerprint" not in encoded
    assert "secret-review-fingerprint" not in encoded
    assert "secret-token" not in encoded
    assert '"action"' not in encoded
    assert '"choices"' not in encoded


def test_path_display_projection_exposes_only_path_access_and_workspace() -> None:
    payload = build_approval_event_payload(
        {
            "id": "sandbox-path-123",
            "namespace": "exec",
            "params": {
                "approvalKind": "sandbox_path",
                "path": "/outside/project",
                "access": "read",
                "workspace": "/workspace/project",
                "fingerprint": "must-not-leak",
                "warning": "Review path access",
            },
            "resolved": False,
        }
    )

    assert payload["tool_name"] == "sandbox_path"
    assert payload["args"] == {
        "path": "/outside/project",
        "access": "read",
        "workspace": "/workspace/project",
    }
    assert payload["warning"] == "Review path access"
    assert "must-not-leak" not in json.dumps(payload)


def test_generic_and_plugin_display_args_are_secret_redacted() -> None:
    generic = build_approval_event_payload(
        {
            "id": "generic-123",
            "namespace": "exec",
            "params": {
                "toolName": "http_request",
                "args": {
                    "url": "https://example.test",
                    "api_key": "sk-abcdefghijklmnopqrstuvwxyz1234",
                    "nested": {"reviewAction": "internal", "value": "visible"},
                },
            },
        }
    )
    plugin = build_approval_event_payload(
        {
            "id": "plugin-123",
            "namespace": "plugin",
            "params": {
                "pluginId": "demo-plugin",
                "permissions": ["filesystem.read", {"token": "plugin-secret"}],
            },
        }
    )

    assert generic["args"] == {
        "url": "https://example.test",
        "api_key": "[REDACTED]",
        "nested": {"value": "visible"},
    }
    assert plugin["args"] == {"permissions": ["filesystem.read", {"token": "[REDACTED]"}]}
    assert "plugin-secret" not in json.dumps(plugin)

    split_header = build_approval_event_payload(
        {
            "id": "argv-secret-123",
            "namespace": "exec",
            "params": {
                "action_kind": "shell.exec",
                "argv": ["curl", "-H", "Authorization:", "Bearer", "hidden-credential"],
            },
        }
    )
    assert "hidden-credential" not in split_header["command"]
    assert "[REDACTED]" in split_header["command"]

    split_snapshot = build_approval_snapshot_item(
        {
            "id": "argv-secret-123",
            "namespace": "exec",
            "params": {
                "action_kind": "shell.exec",
                "argv": ["curl", "-H", "Authorization:", "Bearer", "hidden-credential"],
            },
        },
        default_mode="prompt",
    )
    assert "argv" not in split_snapshot
    assert "hidden-credential" not in json.dumps(split_snapshot)


def test_generic_display_projection_redacts_browser_sensitive_values_without_losing_shape() -> None:
    info = {
        "id": "generic-browser-secrets-123",
        "namespace": "exec",
        "params": {
            "toolName": "http_request",
            "args": {
                "url": "https://example.test/resource",
                "headers": {
                    "Cookie": "session=browser-cookie-secret",
                    "Set-Cookie": "refresh=browser-set-cookie-secret; HttpOnly",
                    "X-Auth-Token": "unknown-auth-header-secret",
                    "X-Trace-Id": "trace-visible",
                },
                "private_key": "private-key-secret",
                "SSHPrivateKey": "ssh-private-key-secret",
                "AUTH": "uppercase-auth-secret",
                "AUTH_TOKEN": "uppercase-auth-token-secret",
                "HTTP_AUTH": "uppercase-http-auth-secret",
                "SSH_PRIVATE_KEY": "uppercase-private-key-secret",
                "requestCookies": ["request-cookie-secret"],
                "header_lines": [
                    "Cookie: header-line-cookie-secret",
                    "Set-Cookie: header-line-set-cookie-secret; Secure",
                    "X-Trace-Id: trace-visible",
                ],
                "tls_material": (
                    "-----BEGIN "
                    "PRIVATE KEY-----\n"
                    "private-key-block-secret\n"
                    "-----END PRIVATE KEY-----"
                ),
                "cookies_enabled": True,
                "cookie_policy": "same-site",
                "authentication_mode": "delegated",
            },
            "command": ("curl -H 'Cookie: command-cookie-secret' https://example.test/resource"),
        },
    }

    push = build_approval_event_payload(info)
    snapshot = build_approval_snapshot_item(info, default_mode="prompt")

    expected_args = {
        "url": "https://example.test/resource",
        "headers": {
            "Cookie": "[REDACTED]",
            "Set-Cookie": "[REDACTED]",
            "X-Auth-Token": "[REDACTED]",
            "X-Trace-Id": "trace-visible",
        },
        "private_key": "[REDACTED]",
        "SSHPrivateKey": "[REDACTED]",
        "AUTH": "[REDACTED]",
        "AUTH_TOKEN": "[REDACTED]",
        "HTTP_AUTH": "[REDACTED]",
        "SSH_PRIVATE_KEY": "[REDACTED]",
        "requestCookies": "[REDACTED]",
        "header_lines": [
            "Cookie: [REDACTED]",
            "Set-Cookie: [REDACTED]",
            "X-Trace-Id: trace-visible",
        ],
        "tls_material": "[REDACTED]",
        "cookies_enabled": True,
        "cookie_policy": "same-site",
        "authentication_mode": "delegated",
    }
    assert push["args"] == expected_args
    assert snapshot["args"] == expected_args
    expected_command = "curl -H 'Cookie: [REDACTED]' https://example.test/resource"
    assert push["command"] == expected_command
    assert snapshot["command"] == expected_command
    encoded = json.dumps({"push": push, "snapshot": snapshot})
    assert "browser-cookie-secret" not in encoded
    assert "browser-set-cookie-secret" not in encoded
    assert "private-key-secret" not in encoded
    assert "ssh-private-key-secret" not in encoded
    assert "request-cookie-secret" not in encoded
    assert "header-line-cookie-secret" not in encoded
    assert "header-line-set-cookie-secret" not in encoded
    assert "private-key-block-secret" not in encoded
    assert "unknown-auth-header-secret" not in encoded
    assert "command-cookie-secret" not in encoded
    assert "uppercase-auth-secret" not in encoded
    assert "uppercase-auth-token-secret" not in encoded
    assert "uppercase-http-auth-secret" not in encoded
    assert "uppercase-private-key-secret" not in encoded

    escaped_command = build_approval_event_payload(
        {
            "id": "escaped-cookie-command-123",
            "namespace": "exec",
            "params": {
                "command": (
                    'curl -H "Cookie: sid=before\\"escaped-cookie-secret" '
                    "https://example.test/resource"
                ),
            },
        }
    )["command"]
    assert escaped_command == ('curl -H "Cookie: [REDACTED]" https://example.test/resource')
    assert "escaped-cookie-secret" not in escaped_command


def test_unknown_sandbox_display_projection_never_exposes_generic_args() -> None:
    info = {
        "id": "sandbox-future-123",
        "namespace": "exec",
        "params": {
            "approvalKind": "sandbox_future",
            "args": {
                "action": {"credential": "sandbox-policy-secret"},
                "cookie_policy": "same-site",
            },
        },
    }

    push = build_approval_event_payload(info)
    snapshot = build_approval_snapshot_item(info, default_mode="prompt")

    assert push["args"] is None
    assert snapshot["args"] is None
    assert "sandbox-policy-secret" not in json.dumps({"push": push, "snapshot": snapshot})


def test_approvals_http_snapshot_uses_safe_display_projection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    queue = ApprovalQueue(db_path=":memory:")
    monkeypatch.setattr(app_module, "get_approval_queue", lambda: queue)
    monkeypatch.setattr(rpc_approvals, "get_approval_queue", lambda: queue)
    try:
        approval_id = queue.request(
            "exec",
            {
                "approvalKind": "sandbox_network",
                "host": "registry.example.test",
                "workspace": "/workspace/project",
                "sessionKey": "agent:main:webchat:demo",
                "fingerprint": "private-fingerprint",
                "reviewer": "user",
                "action": {"credential": "private-secret"},
            },
        )
        app = app_module.create_gateway_app(GatewayConfig())
        with TestClient(app, client=("127.0.0.1", 51000)) as client:
            response = client.get("/api/approvals")

        assert response.status_code == 200, response.text
        item = response.json()["pending"][0]
        assert item["id"] == approval_id
        assert item["toolName"] == "sandbox_network"
        assert item["approvalKind"] == "sandbox_network"
        assert item["args"] == {
            "host": "registry.example.test",
            "workspace": "/workspace/project",
        }
        assert item["warning"] == ""
        assert "params" not in item
        encoded = response.text
        assert "private-fingerprint" not in encoded
        assert "private-secret" not in encoded
        assert '"action"' not in encoded
    finally:
        queue.close()


def test_build_approval_event_payload_maps_sandbox_session_id() -> None:
    payload = build_approval_event_payload(
        {
            "id": "sandbox-shell-123",
            "namespace": "exec",
            "params": {
                "action_kind": "shell.exec",
                "argv": ["exec_command", "rm -f approval-e2e-ok.txt"],
                "session_id": "agent:main:webchat:demo",
            },
            "created_at": 1.0,
            "deadline": 2.0,
            "resolved": False,
            "approved": False,
        }
    )

    assert payload["session_key"] == "agent:main:webchat:demo"


def test_auto_review_approval_does_not_emit_actionable_push() -> None:
    info = {
        "namespace": "exec",
        "params": {"humanActionable": False, "reviewer": "auto_review"},
    }

    assert approval_event_name("requested", info) is None


def test_human_approval_still_emits_actionable_push() -> None:
    info = {
        "namespace": "exec",
        "params": {"humanActionable": True, "reviewer": "user"},
    }

    assert approval_event_name("requested", info) == "exec.approval.requested"
