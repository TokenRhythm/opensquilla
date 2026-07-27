from __future__ import annotations

import pytest

from opensquilla.channels.admission import CHANNEL_ADMIN_VERIFIED_METADATA_KEY
from opensquilla.channels.types import IncomingMessage
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.routing import (
    build_channel_route_envelope,
    build_cli_route_envelope,
    build_web_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_sessions import (
    _apply_run_context_route_metadata,
    _trusted_run_mode_hint,
)
from opensquilla.sandbox.run_context import (
    DomainGrant,
    MountGrant,
    PackageBundleGrant,
    PublicNetworkGrant,
    RunContext,
)
from opensquilla.sandbox.run_mode import RunMode


def _owner_rpc_context(*, is_owner: bool = True) -> RpcContext:
    return RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=is_owner,
            authenticated=True,
        ),
    )


def _mark_verified_channel_admin(envelope) -> None:
    """Model the authenticated ingress result, not adapter metadata."""
    envelope.metadata["principal_is_owner"] = True
    envelope.metadata[CHANNEL_ADMIN_VERIFIED_METADATA_KEY] = True


def test_saved_route_run_mode_wins_over_later_global_full_default() -> None:
    envelope = build_cli_route_envelope(
        session_key="agent:main:cli",
        run_mode="standard",
    )

    ctx = tool_context_from_envelope(
        envelope,
        is_owner=True,
        default_elevated="full",
    )

    assert ctx.run_mode == "standard"
    assert ctx.elevated is None


def test_channel_route_preserves_default_run_mode_for_verified_admin() -> None:
    envelope = build_channel_route_envelope(
        IncomingMessage(sender_id="u1", channel_id="c1", content="hello"),
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    user_ctx = tool_context_from_envelope(envelope, is_owner=False)
    _mark_verified_channel_admin(envelope)
    admin_ctx = tool_context_from_envelope(
        envelope,
        is_owner=True,
        default_elevated="full",
    )

    # Administrator identity widens the tool surface, not the session's
    # execution policy. The same default applies to the WebUI owner.
    assert user_ctx.run_mode == "trusted"
    assert user_ctx.elevated is None
    assert admin_ctx.run_mode == "trusted"
    assert admin_ctx.elevated is None


def test_channel_route_preserves_explicit_trusted_choice_for_owner() -> None:
    envelope = build_channel_route_envelope(
        IncomingMessage(sender_id="u1", channel_id="c1", content="hello"),
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    _mark_verified_channel_admin(envelope)
    # A saved per-session /sandbox trusted choice remains trusted for a
    # verified channel administrator, just as it does in the WebUI.
    _apply_run_context_route_metadata(
        envelope,
        RunContext(run_mode=RunMode.TRUSTED, source="saved"),
        principal_is_owner=True,
    )

    admin_ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert envelope.metadata["run_mode_explicit"] is True
    assert admin_ctx.run_mode == "trusted"
    assert admin_ctx.elevated is None


def test_channel_route_default_run_context_matches_sandbox_context_for_owner() -> None:
    envelope = build_channel_route_envelope(
        IncomingMessage(sender_id="u1", channel_id="c1", content="hello"),
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    _mark_verified_channel_admin(envelope)
    # A default (unsaved) run context must not count as an explicit choice.
    _apply_run_context_route_metadata(
        envelope,
        RunContext(run_mode=RunMode.TRUSTED, source="default"),
        principal_is_owner=True,
    )

    admin_ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert envelope.metadata["run_mode_explicit"] is False
    assert admin_ctx.run_mode == "trusted"
    assert admin_ctx.elevated is None
    assert admin_ctx.sandbox_run_context is not None
    assert admin_ctx.sandbox_run_context.run_mode == RunMode.TRUSTED


@pytest.mark.parametrize("run_mode", list(RunMode))
def test_verified_channel_admin_matches_web_owner_run_context(run_mode: RunMode) -> None:
    """A channel transport must not rewrite the owner's execution policy."""

    channel_envelope = build_channel_route_envelope(
        IncomingMessage(sender_id="u1", channel_id="c1", content="hello"),
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    _mark_verified_channel_admin(channel_envelope)
    web_envelope = build_web_route_envelope(
        session_key="agent:main:webchat:owner",
        agent_id="main",
        principal_is_owner=True,
    )
    channel_run_context = RunContext(run_mode=run_mode, source="default")
    web_run_context = RunContext(run_mode=run_mode, source="default")
    _apply_run_context_route_metadata(
        channel_envelope,
        channel_run_context,
        principal_is_owner=True,
    )
    _apply_run_context_route_metadata(
        web_envelope,
        web_run_context,
        principal_is_owner=True,
    )

    channel_ctx = tool_context_from_envelope(channel_envelope, is_owner=True)
    web_ctx = tool_context_from_envelope(web_envelope, is_owner=True)

    assert channel_ctx.run_mode == web_ctx.run_mode == run_mode.value
    assert channel_ctx.elevated == web_ctx.elevated
    assert channel_ctx.sandbox_run_context is not None
    assert web_ctx.sandbox_run_context is not None
    assert channel_ctx.sandbox_run_context.run_mode == web_ctx.sandbox_run_context.run_mode


def test_channel_owner_can_use_explicit_full_route_metadata() -> None:
    envelope = build_channel_route_envelope(
        IncomingMessage(sender_id="u1", channel_id="c1", content="hello"),
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )
    envelope.metadata["run_mode"] = "full"

    user_ctx = tool_context_from_envelope(envelope, is_owner=False)
    _mark_verified_channel_admin(envelope)
    admin_ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert user_ctx.run_mode == "trusted"
    assert user_ctx.elevated is None
    assert admin_ctx.run_mode == "full"
    assert admin_ctx.elevated == "full"


def test_unstamped_channel_owner_context_stays_restricted() -> None:
    envelope = build_channel_route_envelope(
        IncomingMessage(sender_id="u1", channel_id="c1", content="hello"),
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    ctx = tool_context_from_envelope(envelope, is_owner=True, default_elevated="full")

    assert ctx.is_owner is False
    assert ctx.channel_admin_verified is False
    assert ctx.run_mode == "trusted"
    assert ctx.elevated is None


def test_route_metadata_hydrates_full_sandbox_run_context() -> None:
    envelope = build_cli_route_envelope(
        session_key="agent:main:cli",
        run_mode="standard",
    )
    run_context = RunContext(
        run_mode=RunMode.STANDARD,
        domains=(DomainGrant(domain="pypi.org"),),
        bundles=(
            PackageBundleGrant(bundle_id="python-package-install", scope="chat"),
            PackageBundleGrant(bundle_id="node-package-install", source="disabled"),
        ),
    )

    _apply_run_context_route_metadata(
        envelope,
        run_context,
        principal_is_owner=True,
    )
    ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert envelope.metadata["run_mode"] == "standard"
    assert envelope.metadata["sandbox_mounts"] == []
    assert envelope.metadata["sandbox_run_context"]["domains"] == [
        {"domain": "pypi.org", "scope": "chat", "source": "manual"}
    ]
    assert envelope.metadata["sandbox_run_context"]["bundles"] == [
        {
            "bundle_id": "python-package-install",
            "scope": "chat",
            "source": "manual",
        },
        {
            "bundle_id": "node-package-install",
            "scope": "workspace",
            "source": "disabled",
        },
    ]
    assert ctx.run_mode == "standard"
    assert isinstance(ctx.sandbox_run_context, RunContext)
    assert [grant.domain for grant in ctx.sandbox_run_context.domains] == ["pypi.org"]
    assert [
        (grant.bundle_id, grant.scope, grant.source)
        for grant in ctx.sandbox_run_context.bundles
    ] == [
        ("python-package-install", "chat", "manual"),
        ("node-package-install", "workspace", "disabled"),
    ]


def test_fresh_route_metadata_preserves_user_scope_grants_for_execution(
    tmp_path,
) -> None:
    from opensquilla.sandbox.integration import _session_mounts_for_policy
    from opensquilla.sandbox.network_guard import decide_network_access
    from opensquilla.tools.types import current_tool_context

    workspace = tmp_path / "workspace"
    chat_mount = tmp_path / "chat-mount"
    user_mount = tmp_path / "user-mount"
    legacy_mount = tmp_path / "legacy-mount"
    for path in (workspace, chat_mount, user_mount, legacy_mount):
        path.mkdir()
    envelope = build_cli_route_envelope(
        session_key="agent:main:cli",
        run_mode="standard",
    )
    run_context = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        mounts=(
            MountGrant(path=str(chat_mount), access="ro", scope="chat"),
            MountGrant(path=str(user_mount), access="rw", scope="workspace"),
        ),
        domains=(
            DomainGrant(domain="chat.example", scope="chat"),
            DomainGrant(domain="user.example", scope="workspace"),
        ),
        public_network=(
            PublicNetworkGrant(scope="workspace", source="manual"),
        ),
    )

    _apply_run_context_route_metadata(
        envelope,
        run_context,
        principal_is_owner=True,
    )
    ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert envelope.metadata["sandbox_run_context"]["mounts"] == [
        {"path": str(chat_mount), "access": "ro", "scope": "chat"},
        {"path": str(user_mount), "access": "rw", "scope": "workspace"},
    ]
    assert envelope.metadata["sandbox_mounts"] == [
        {"path": str(chat_mount), "access": "ro", "scope": "chat"},
        {"path": str(user_mount), "access": "rw", "scope": "workspace"},
    ]
    assert isinstance(ctx.sandbox_run_context, RunContext)
    assert [(grant.path, grant.scope) for grant in ctx.sandbox_run_context.mounts] == [
        (str(chat_mount), "chat"),
        (str(user_mount), "workspace"),
    ]
    assert [
        (grant.domain, grant.scope) for grant in ctx.sandbox_run_context.domains
    ] == [
        ("chat.example", "chat"),
        ("user.example", "workspace"),
    ]
    assert ctx.sandbox_run_context.public_network == (
        PublicNetworkGrant(scope="workspace", source="manual"),
    )
    assert ctx.sandbox_mounts == [
        {"path": str(chat_mount), "access": "ro", "scope": "chat"},
        {"path": str(user_mount), "access": "rw", "scope": "workspace"},
    ]
    assert decide_network_access("user.example", ctx.sandbox_run_context).status == "allow"
    public_network_decision = decide_network_access(
        "unknown-route-metadata.test",
        ctx.sandbox_run_context,
    )
    assert public_network_decision.status == "allow"
    assert public_network_decision.source == "public_network:user"

    token = current_tool_context.set(ctx)
    try:
        policy_mounts = _session_mounts_for_policy(workspace)
    finally:
        current_tool_context.reset(token)

    assert [(str(mount.host_path), mount.mode) for mount in policy_mounts] == [
        (str(chat_mount), "ro"),
        (str(user_mount), "rw"),
    ]

    legacy_envelope = build_cli_route_envelope(
        session_key="agent:main:cli",
        run_mode="standard",
    )
    legacy_payload = run_context.to_origin_payload()
    legacy_envelope.metadata["sandbox_run_context"] = legacy_payload
    legacy_envelope.metadata["sandbox_mounts"] = legacy_payload["mounts"] + [
        {"path": str(legacy_mount), "access": "rw"}
    ]

    legacy_ctx = tool_context_from_envelope(legacy_envelope, is_owner=True)

    assert legacy_ctx.sandbox_mounts == [
        {"path": str(chat_mount), "access": "ro", "scope": "chat"}
    ]
    assert isinstance(legacy_ctx.sandbox_run_context, RunContext)
    assert [
        (grant.path, grant.scope) for grant in legacy_ctx.sandbox_run_context.mounts
    ] == [(str(chat_mount), "chat")]
    assert [
        (grant.domain, grant.scope) for grant in legacy_ctx.sandbox_run_context.domains
    ] == [("chat.example", "chat")]
    assert legacy_ctx.sandbox_run_context.public_network == ()
    token = current_tool_context.set(legacy_ctx)
    try:
        legacy_policy_mounts = _session_mounts_for_policy(workspace)
    finally:
        current_tool_context.reset(token)
    assert [(str(mount.host_path), mount.mode) for mount in legacy_policy_mounts] == [
        (str(chat_mount), "ro")
    ]


def test_policy_mounts_use_live_run_context_when_legacy_mount_metadata_is_stale(
    tmp_path,
) -> None:
    from opensquilla.sandbox.integration import _session_mounts_for_policy
    from opensquilla.tools.types import ToolContext, current_tool_context

    workspace = tmp_path / "workspace"
    approved_mount = tmp_path / "approved-mount"
    workspace.mkdir()
    approved_mount.mkdir()

    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(workspace),
        session_key="agent:main:cli",
        sandbox_mounts=[],
        sandbox_run_context=RunContext(
            run_mode=RunMode.TRUSTED,
            workspace=str(workspace),
            mounts=(MountGrant(path=str(approved_mount), access="ro", scope="chat"),),
        ),
    )

    token = current_tool_context.set(ctx)
    try:
        policy_mounts = _session_mounts_for_policy(workspace)
    finally:
        current_tool_context.reset(token)

    assert [
        (str(mount.host_path), str(mount.sandbox_path), mount.mode)
        for mount in policy_mounts
    ] == [
        (str(approved_mount), str(approved_mount), "ro"),
    ]


def test_policy_mounts_treat_live_empty_run_context_as_authoritative(
    tmp_path,
) -> None:
    from opensquilla.sandbox.integration import _session_mounts_for_policy
    from opensquilla.tools.types import ToolContext, current_tool_context

    workspace = tmp_path / "workspace"
    removed_mount = tmp_path / "removed-mount"
    workspace.mkdir()
    removed_mount.mkdir()

    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(workspace),
        session_key="agent:main:cli",
        sandbox_mounts=[{"path": str(removed_mount), "access": "ro"}],
        sandbox_run_context=RunContext(
            run_mode=RunMode.STANDARD,
            workspace=str(workspace),
            mounts=(),
        ),
    )

    token = current_tool_context.set(ctx)
    try:
        policy_mounts = _session_mounts_for_policy(workspace)
    finally:
        current_tool_context.reset(token)

    assert policy_mounts == ()


def test_invalid_route_run_context_metadata_is_ignored() -> None:
    envelope = build_cli_route_envelope(
        session_key="agent:main:cli",
        run_mode="standard",
    )
    envelope.metadata["sandbox_run_context"] = {"run_mode": "unknown", "domains": "pypi.org"}

    ctx = tool_context_from_envelope(envelope, is_owner=True)

    assert ctx.sandbox_run_context is None


def test_legacy_owner_elevated_aliases_map_to_trusted_run_mode() -> None:
    ctx = _owner_rpc_context(is_owner=True)

    assert _trusted_run_mode_hint(ctx, {"elevated": "on"}) == RunMode.TRUSTED
    assert _trusted_run_mode_hint(ctx, {"elevated": "bypass"}) == RunMode.TRUSTED


def test_legacy_owner_full_elevated_alias_maps_to_full_run_mode() -> None:
    ctx = _owner_rpc_context(is_owner=True)

    assert _trusted_run_mode_hint(ctx, {"elevated": "full"}) == RunMode.FULL


def test_legacy_elevated_aliases_are_ignored_for_non_owner() -> None:
    ctx = _owner_rpc_context(is_owner=False)

    assert _trusted_run_mode_hint(ctx, {"elevated": "bypass"}) is None
    assert _trusted_run_mode_hint(ctx, {"elevated": "full"}) is None
