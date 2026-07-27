from __future__ import annotations

from types import SimpleNamespace

from opensquilla.channels.types import IncomingMessage
from opensquilla.gateway.boot import _task_runtime_envelope_owner
from opensquilla.gateway.routing import (
    build_channel_route_envelope,
    build_cli_route_envelope,
    build_cron_route_envelope,
    build_subagent_route_envelope,
    build_web_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.scheduler.handlers import _build_cron_tool_context
from opensquilla.scheduler.types import CronJob, SessionTarget
from opensquilla.tools.policy import ToolSurfaceCapabilities, resolve_runtime_tool_surface
from opensquilla.tools.types import CallerKind, InteractionMode


def test_route_envelopes_assign_expected_interaction_modes() -> None:
    channel_msg = IncomingMessage(sender_id="u1", channel_id="c1", content="hi")
    cron_job = SimpleNamespace(id="job-1", name="demo")

    cases = [
        (
            build_cli_route_envelope(session_key="agent:main:cli"),
            CallerKind.CLI,
            InteractionMode.INTERACTIVE,
        ),
        (
            build_cli_route_envelope(
                session_key="agent:main:auto",
                interaction_mode=InteractionMode.UNATTENDED,
            ),
            CallerKind.CLI,
            InteractionMode.UNATTENDED,
        ),
        (
            build_web_route_envelope(session_key="agent:main:web"),
            CallerKind.WEB,
            InteractionMode.INTERACTIVE,
        ),
        (
            build_channel_route_envelope(
                channel_msg,
                session_key="telegram:dm:u1",
                session_prefix="telegram",
            ),
            CallerKind.CHANNEL,
            InteractionMode.UNATTENDED,
        ),
        (
            build_cron_route_envelope(cron_job, session_key="cron:job-1"),
            CallerKind.CRON,
            InteractionMode.UNATTENDED,
        ),
        (
            build_subagent_route_envelope(
                session_key="subagent:parent:child",
                parent_session_key="agent:main:parent",
            ),
            CallerKind.SUBAGENT,
            InteractionMode.UNATTENDED,
        ),
    ]

    for envelope, expected_kind, expected_mode in cases:
        ctx = tool_context_from_envelope(envelope)
        assert ctx.caller_kind is expected_kind
        assert ctx.interaction_mode is expected_mode


def test_unattended_cli_denies_runtime_dependent_tools_but_keeps_session_reads() -> None:
    envelope = build_cli_route_envelope(
        session_key="agent:main:auto",
        interaction_mode=InteractionMode.UNATTENDED,
    )

    ctx = resolve_runtime_tool_surface(
        tool_context_from_envelope(envelope, is_owner=True),
        capabilities=ToolSurfaceCapabilities(session_manager=True),
    )

    assert "sessions_spawn" in ctx.denied_tools
    assert "gateway" in ctx.denied_tools
    assert "sessions_list" not in ctx.denied_tools
    assert "sessions_history" not in ctx.denied_tools
    assert "session_status" not in ctx.denied_tools


def test_default_elevated_mode_only_keeps_full_for_owner_tool_context() -> None:
    envelope = build_cli_route_envelope(session_key="agent:main:cli")

    bypass_ctx = tool_context_from_envelope(
        envelope,
        is_owner=True,
        default_elevated="bypass",
    )
    owner_ctx = tool_context_from_envelope(
        envelope,
        is_owner=True,
        default_elevated="full",
    )
    non_owner_ctx = tool_context_from_envelope(
        envelope,
        is_owner=False,
        default_elevated="full",
    )

    assert bypass_ctx.elevated is None
    assert owner_ctx.elevated == "full"
    assert owner_ctx.run_mode == "full"
    assert non_owner_ctx.elevated is None


def test_cron_default_elevated_resolves_at_context_build_time() -> None:
    job = CronJob(
        id="job-owner",
        name="owner",
        session_target=SessionTarget.ISOLATED,
        creator_is_owner=True,
    )
    default_mode = {"value": "full"}

    first_ctx = _build_cron_tool_context(
        "agent",
        job,
        default_elevated=lambda: default_mode["value"],
    )
    default_mode["value"] = "bypass"
    second_ctx = _build_cron_tool_context(
        "agent",
        job,
        default_elevated=lambda: default_mode["value"],
    )

    assert first_ctx.elevated == "full"
    assert first_ctx.run_mode == "full"
    assert second_ctx.elevated is None


def test_route_run_mode_metadata_reaches_tool_context() -> None:
    envelope = build_cli_route_envelope(
        session_key="agent:main:cli",
        run_mode="trusted",
    )

    ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert ctx.run_mode == "trusted"
    assert ctx.elevated is None


def test_non_owner_full_run_mode_metadata_coerces_to_trusted() -> None:
    envelope = build_cli_route_envelope(
        session_key="agent:main:cli",
        run_mode="full",
    )

    ctx = tool_context_from_envelope(envelope, is_owner=False)

    assert ctx.run_mode == "trusted"
    assert ctx.elevated is None


def test_owner_subagent_route_preserves_full_host_run_context() -> None:
    run_context = RunContext(
        run_mode=RunMode.FULL,
        workspace="/tmp/opensquilla-workspace",
    )

    envelope = build_subagent_route_envelope(
        session_key="agent:main:subagent:child",
        parent_session_key="agent:main:webchat:parent",
        run_mode="full",
        sandbox_run_context=run_context,
        principal_is_owner=True,
    )
    ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert envelope.metadata["principal_is_owner"] is True
    assert envelope.metadata["run_mode"] == "full"
    assert envelope.metadata["elevated"] == "full"
    assert envelope.metadata["sandbox_run_context"]["run_mode"] == "full"
    assert envelope.sandbox_run_context_fresh is True
    assert ctx.caller_kind is CallerKind.SUBAGENT
    assert ctx.run_mode == "full"
    assert ctx.elevated == "full"
    assert ctx.sandbox_run_context is not None
    assert ctx.sandbox_run_context.run_mode is RunMode.FULL


def test_owner_cron_route_carries_owner_principal_for_task_runtime() -> None:
    cron_job = SimpleNamespace(id="job-owner", name="owner", creator_is_owner=True)

    envelope = build_cron_route_envelope(cron_job, session_key="cron:job-owner")

    assert envelope.metadata["principal_is_owner"] is True
    assert _task_runtime_envelope_owner(envelope) is True


def test_owner_cron_route_uses_owner_grade_tool_boundary() -> None:
    cron_job = SimpleNamespace(id="job-owner", name="owner", creator_is_owner=True)
    envelope = build_cron_route_envelope(cron_job, session_key="cron:job-owner")

    ctx = tool_context_from_envelope(
        envelope,
        is_owner=_task_runtime_envelope_owner(envelope),
    )

    assert ctx.caller_kind is CallerKind.CRON
    assert ctx.is_owner is True
    assert ctx.allowed_tools is None
    assert "exec_command" not in ctx.denied_tools
    assert "write_file" not in ctx.denied_tools


def test_non_owner_cron_route_keeps_restricted_tool_boundary() -> None:
    cron_job = SimpleNamespace(id="job-user", name="user")
    envelope = build_cron_route_envelope(cron_job, session_key="cron:job-user")

    ctx = tool_context_from_envelope(envelope)

    assert ctx.caller_kind is CallerKind.CRON
    assert ctx.is_owner is False
    assert ctx.allowed_tools is not None
    assert "exec_command" not in ctx.allowed_tools
    assert "exec_command" in ctx.denied_tools
