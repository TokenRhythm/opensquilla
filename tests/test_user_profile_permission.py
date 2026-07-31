from __future__ import annotations

from opensquilla.gateway.boot import _user_profile_permission_snapshot
from opensquilla.gateway.config import GatewayConfig, ToolsConfig
from opensquilla.squilla_router.user_profile.permission import (
    build_permission_snapshot,
)


def test_permission_snapshot_uses_live_gateway_values_and_tools() -> None:
    snapshot = build_permission_snapshot(
        baseline={
            "allow_models": [],
            "deny_models": [],
            "risk_allowlist": ["low", "medium", "high"],
        },
        live_override={
            "allow_models": ["model-a"],
            "deny_models": ["model-b"],
            "risk_allowlist": ["low", "medium"],
        },
        allowed_tools=["memory_search", "session_status", "memory_search"],
    )

    assert snapshot == {
        "allow_models": ["model-a"],
        "deny_models": ["model-b"],
        "allow_tools": ["memory_search", "session_status"],
        "risk_allowlist": ["low", "medium"],
    }


def test_permission_snapshot_keeps_baseline_when_no_live_override() -> None:
    snapshot = build_permission_snapshot(
        baseline={
            "allow_models": ["model-a"],
            "deny_models": [],
            "risk_allowlist": ["low"],
        },
        live_override=None,
        allowed_tools=(),
    )

    assert snapshot == {
        "allow_models": ["model-a"],
        "deny_models": [],
        "allow_tools": [],
        "risk_allowlist": ["low"],
    }


def test_gateway_snapshot_records_effective_tool_policy() -> None:
    class _Registry:
        @staticmethod
        def list_names() -> list[str]:
            return ["exec_command", "memory_search", "session_status"]

    config = GatewayConfig(tools=ToolsConfig(profile="minimal"))

    snapshot = _user_profile_permission_snapshot(config, _Registry(), "main")

    assert snapshot == {
        "allow_models": [],
        "deny_models": [],
        "allow_tools": ["session_status"],
        "risk_allowlist": ["low", "medium", "high"],
    }
