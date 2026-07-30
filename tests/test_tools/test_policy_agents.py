from __future__ import annotations

from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig
from opensquilla.gateway.routing import build_cron_route_envelope, tool_context_from_envelope
from opensquilla.scheduler.types import CronJob
from opensquilla.tools.policy import apply_tool_policy_from_config
from opensquilla.tools.types import (
    CRON_AGENT_DENY,
    SUBAGENT_TOOL_DENY,
    CallerKind,
    ToolContext,
)


def test_tool_policy_reads_direct_gateway_agents_list() -> None:
    cfg = GatewayConfig(
        agents=[
            AgentEntryConfig(
                id="ops",
                tools={"profile": "minimal", "also_allow": ["memory_search"]},
            )
        ]
    )
    ctx = ToolContext(agent_id="ops")

    result = apply_tool_policy_from_config(
        ctx,
        available_tools=["session_status", "memory_search", "exec_command"],
        config=cfg,
    )

    assert result.allowed_tools == {"session_status", "memory_search"}


def test_cron_route_tool_policy_can_only_narrow_or_extend_cron_baseline() -> None:
    job = CronJob(
        id="policy",
        name="Policy",
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )

    envelope = build_cron_route_envelope(
        job,
        session_key="cron:policy:run:1",
        agent_id="main",
    )
    result = tool_context_from_envelope(envelope)

    assert envelope.metadata["tool_policy"] == job.tool_policy
    assert result.caller_kind is CallerKind.CRON
    assert result.allowed_tools == {"session_status"}
    assert "web_fetch" in result.denied_tools
    assert "exec_command" in result.denied_tools


def test_owner_cron_route_does_not_apply_non_owner_cron_allowlist() -> None:
    job = CronJob(
        id="owner-policy",
        name="Owner Policy",
        creator_is_owner=True,
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )

    envelope = build_cron_route_envelope(
        job,
        session_key="cron:owner-policy:run:1",
        agent_id="main",
    )
    result = tool_context_from_envelope(envelope, is_owner=True)

    assert result.caller_kind is CallerKind.CRON
    assert result.is_owner is True
    assert result.allowed_tools is None
    assert result.tool_policy == job.tool_policy
    assert "exec_command" not in result.denied_tools


def test_policy_deny_lists_do_not_reference_removed_agent_wrapper_tools() -> None:
    assert "spawn_subagent" not in SUBAGENT_TOOL_DENY
    assert "send_message" not in SUBAGENT_TOOL_DENY
    assert "spawn_subagent" not in CRON_AGENT_DENY
    assert "send_message" not in CRON_AGENT_DENY


def test_messaging_group_does_not_revive_removed_agent_send_wrapper() -> None:
    cfg = GatewayConfig(tools={"profile": "messaging"})
    ctx = ToolContext(agent_id="main")

    result = apply_tool_policy_from_config(
        ctx,
        available_tools=["message", "send_message", "sessions_send", "session_status"],
        config=cfg,
    )

    assert result.allowed_tools is not None
    assert "message" in result.allowed_tools
    assert "sessions_send" in result.allowed_tools
    assert "send_message" not in result.allowed_tools


def test_channel_media_group_expands_safe_file_authoring_tools() -> None:
    cfg = {
        "channels": {
            "feishu": {
                "groups": {
                    "oc_demo": {
                        "tools": {"profile": "minimal", "also_allow": ["channel:media"]}
                    }
                }
            }
        }
    }
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.CHANNEL,
        channel_kind="feishu",
        channel_id="oc_demo",
        sender_id="ou_user",
    )

    result = apply_tool_policy_from_config(
        ctx,
        available_tools=[
            "session_status",
            "create_csv",
            "create_xlsx",
            "create_pptx",
            "create_pdf_report",
            "execute_code",
        ],
        config=cfg,
    )

    assert result.allowed_tools == {
        "session_status",
        "create_csv",
        "create_xlsx",
        "create_pptx",
        "create_pdf_report",
    }


def test_channel_perm_group_is_empty_until_explicit_tools_exist() -> None:
    cfg = {
        "channels": {
            "feishu": {
                "groups": {
                    "oc_demo": {
                        "tools": {"profile": "minimal", "also_allow": ["channel:perm"]}
                    }
                }
            }
        }
    }
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.CHANNEL,
        channel_kind="feishu",
        channel_id="oc_demo",
        sender_id="ou_user",
    )

    result = apply_tool_policy_from_config(
        ctx,
        available_tools=["session_status", "feishu_permission_grant"],
        config=cfg,
    )

    assert result.allowed_tools == {"session_status"}


def test_channel_sender_policy_can_enable_drive_for_one_sender() -> None:
    cfg = {
        "channels": {
            "feishu": {
                "groups": {
                    "oc_demo": {
                        "tools": {
                            "profile": "minimal",
                            "toolsBySender": {
                                "id:ou_allowed": {"also_allow": ["channel:drive"]}
                            },
                        }
                    }
                }
            }
        }
    }
    available = ["session_status", "create_pptx"]

    allowed = apply_tool_policy_from_config(
        ToolContext(
            is_owner=False,
            caller_kind=CallerKind.CHANNEL,
            channel_kind="feishu",
            channel_id="oc_demo",
            sender_id="ou_allowed",
        ),
        available_tools=available,
        config=cfg,
    )
    other = apply_tool_policy_from_config(
        ToolContext(
            is_owner=False,
            caller_kind=CallerKind.CHANNEL,
            channel_kind="feishu",
            channel_id="oc_demo",
            sender_id="ou_other",
        ),
        available_tools=available,
        config=cfg,
    )

    assert allowed.allowed_tools == {"session_status", "create_pptx"}
    assert other.allowed_tools == {"session_status"}
