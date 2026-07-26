"""Regression tests for the dispatch policy refactor surface-hardening pass.

Two boundaries are exercised here:

1. Untrusted CHANNEL callers (and anonymous callers with no
   :class:`ToolContext`) must not be able to enumerate the registry by
   probing tool names. The registry-miss envelope they see must be opaque,
   while the structured log retains the actual tool name for operators.

2. The :class:`PermissionMatrixPolicy` must reject a generic owner CHANNEL
   context unless it also carries the authenticated ingress marker. Only that
   marker can promote a configured channel admin through the ``ADMIN_ONLY``
   gate.
"""

from __future__ import annotations

import json

import pytest
import structlog.testing

from opensquilla.engine.types import ToolCall
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    ToolSpec,
    current_tool_context,
)


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def some_tool() -> str:
        return "ok"

    registry.register(
        ToolSpec(name="some_real_tool", description="real", parameters={}),
        some_tool,
    )
    return registry


_PROBE_TOOL_NAME = "definitely_not_a_real_tool_xyz"


@pytest.mark.asyncio
async def test_registry_miss_for_channel_caller_is_opaque() -> None:
    handler = build_tool_handler(_build_registry())
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id="main",
        session_key="agent:main:hardening",
    )
    token = current_tool_context.set(ctx)
    try:
        with structlog.testing.capture_logs() as captured:
            result = await handler(
                ToolCall(
                    tool_use_id="tc-opaque-1",
                    tool_name=_PROBE_TOOL_NAME,
                    arguments={},
                )
            )
    finally:
        current_tool_context.reset(token)

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["error_class"] == "PolicyDenied"
    assert payload["status"] == "error"
    assert payload["user_message"] == "Tool unavailable for this surface."
    # The tool name must NOT appear in any user-visible field.
    assert _PROBE_TOOL_NAME not in payload["user_message"]

    # Operators must still be able to debug the miss via structured logs.
    miss_events = [e for e in captured if e["event"] == "dispatch.registry_miss"]
    assert miss_events, "dispatch.registry_miss must be logged"
    event = miss_events[0]
    assert event["tool"] == _PROBE_TOOL_NAME
    assert event["untrusted_caller"] is True
    assert event["is_skill"] is False
    assert event["session_key"] == "agent:main:hardening"


@pytest.mark.asyncio
async def test_registry_miss_for_anonymous_caller_is_opaque() -> None:
    """No ``ToolContext`` at all is treated as untrusted for the same reason."""
    handler = build_tool_handler(_build_registry())

    with structlog.testing.capture_logs() as captured:
        result = await handler(
            ToolCall(
                tool_use_id="tc-opaque-2",
                tool_name=_PROBE_TOOL_NAME,
                arguments={},
            )
        )

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["error_class"] == "PolicyDenied"
    assert _PROBE_TOOL_NAME not in payload["user_message"]
    miss_events = [e for e in captured if e["event"] == "dispatch.registry_miss"]
    assert miss_events and miss_events[0]["tool"] == _PROBE_TOOL_NAME


@pytest.mark.asyncio
async def test_registry_miss_for_channel_skill_collision_is_opaque() -> None:
    """Even the skill-collision branch must not echo the probed name."""
    handler = build_tool_handler(
        _build_registry(),
        known_skill_names={"my_skill"},
    )
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id="main",
        session_key="agent:main:hardening",
    )
    token = current_tool_context.set(ctx)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="tc-opaque-3",
                tool_name="my_skill",
                arguments={},
            )
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result.content)
    assert payload["error_class"] == "PolicyDenied"
    assert "my_skill" not in payload["user_message"]


@pytest.mark.asyncio
async def test_registry_miss_for_cli_caller_preserves_descriptive_envelope() -> None:
    """Trusted CLI callers must keep the actionable ToolNotFound message."""
    handler = build_tool_handler(_build_registry())
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        agent_id="main",
        session_key="cli:main:hardening",
    )
    token = current_tool_context.set(ctx)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="tc-cli-miss",
                tool_name=_PROBE_TOOL_NAME,
                arguments={},
            )
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result.content)
    assert payload["error_class"] == "ToolNotFound"
    assert _PROBE_TOOL_NAME in payload["user_message"]


@pytest.mark.asyncio
async def test_registry_miss_for_owner_channel_preserves_descriptive_envelope() -> None:
    """Owner CHANNEL callers (already cleared upstream) keep the verbose envelope."""
    handler = build_tool_handler(_build_registry())
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id="main",
        session_key="agent:main:hardening",
    )
    token = current_tool_context.set(ctx)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="tc-owner-miss",
                tool_name=_PROBE_TOOL_NAME,
                arguments={},
            )
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result.content)
    assert payload["error_class"] == "ToolNotFound"
    assert _PROBE_TOOL_NAME in payload["user_message"]


def _registry_with(name: str) -> ToolRegistry:
    registry = ToolRegistry()

    async def handler() -> str:
        return "ok"

    registry.register(ToolSpec(name=name, description=name, parameters={}), handler)
    return registry


@pytest.mark.asyncio
async def test_permission_matrix_rejects_unverified_owner_channel_context() -> None:
    """A leaked ``is_owner=True`` on a CHANNEL ctx must not promote to operator.

    ``git_push`` is ``ADMIN_ONLY`` in the permission matrix. A genuine
    operator principal would receive ``operator_override`` and be allowed,
    so any test that sees an allow here would prove the clamp had failed.
    """
    handler = build_tool_handler(_registry_with("git_push"))
    ctx = ToolContext(
        is_owner=True,  # Simulated leak from a buggy upstream constructor.
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id="main",
        session_key="agent:main:hardening",
        # Explicit allowlist bypasses the profile gate so the matrix gate
        # is the one we observe.
        allowed_tools={"git_push"},
    )
    token = current_tool_context.set(ctx)
    try:
        with structlog.testing.capture_logs() as captured:
            result = await handler(
                ToolCall(
                    tool_use_id="tc-matrix-clamp",
                    tool_name="git_push",
                    arguments={},
                )
            )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result.content)
    assert result.is_error is True
    assert payload["error_class"] == "UnsupportedSurface"
    # operator_override would NEVER appear in a user-visible reason; matrix
    # block must fire.
    assert "operator_override" not in payload["user_message"]
    matrix_blocks = [
        e for e in captured if e["event"] == "dispatch.permission_matrix_block"
    ]
    assert matrix_blocks, "permission matrix must record the block"
    assert matrix_blocks[0]["reason"].startswith("admin_only_denied_in_")


@pytest.mark.asyncio
async def test_permission_matrix_allows_authenticated_channel_admin_context() -> None:
    """The ingress-verified admin marker is sufficient for admin-only tools."""
    handler = build_tool_handler(_registry_with("git_push"))
    ctx = ToolContext(
        is_owner=True,
        channel_admin_verified=True,
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id="main",
        session_key="agent:main:hardening",
        channel_id="oc_channel",
    )
    token = current_tool_context.set(ctx)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="tc-matrix-admin",
                tool_name="git_push",
                arguments={},
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result.is_error is False
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_permission_matrix_uses_webui_source_for_owner_admin_tools() -> None:
    """Authenticated Web UI turns should use the Web UI permission surface.

    Webchat sessions carry ``channel_kind='webchat'`` for display/routing,
    while the trusted surface is recorded as ``source_kind='webui'``. The
    permission matrix must not collapse those turns to the DM surface or
    admin-only workspace tools are denied even for authenticated operators.
    """
    handler = build_tool_handler(_registry_with("write_file"))
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.INTERACTIVE,
        agent_id="main",
        session_key="agent:main:webchat:hardening",
        channel_kind="webchat",
        source_kind="webui",
    )
    token = current_tool_context.set(ctx)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="tc-webui-write",
                tool_name="write_file",
                arguments={},
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result.is_error is False
    assert result.content == "ok"
