from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.routing import (
    build_web_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sessions import (
    _guest_profile_for_principal,
    _is_remote_web_guest,
    _trusted_run_mode_hint,
)
from opensquilla.sandbox.guest_profile import (
    GuestProfileFactory,
    cleanup_guest_profile_root,
)
from opensquilla.tools.builtin.shell import _base_shell_environment
from opensquilla.tools.types import ToolContext, current_tool_context
from opensquilla.tools.visibility import guest_safe_tool_allowlist


def _guest_principal(*, invalid: bool = False) -> Principal:
    return Principal(
        role="operator",
        scopes=frozenset(),
        is_owner=False,
        authenticated=False,
        capabilities=frozenset({"guest.safe"}),
        auth_state="invalid" if invalid else "guest",
    )


def _directory_alias(alias: Path, target: Path) -> None:
    try:
        alias.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory aliases unavailable: {exc}")
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"directory aliases unavailable: {result.stderr or result.stdout}")


def test_guest_boundary_cannot_be_bypassed_by_spoofing_source_kind() -> None:
    principal = _guest_principal()

    assert _is_remote_web_guest(principal, {"caller_kind": "web"}) is True
    assert _is_remote_web_guest(principal, {"caller_kind": "cli"}) is True
    assert _is_remote_web_guest(principal, {"channel_kind": "channel"}) is True


@pytest.mark.parametrize("invalid", [False, True])
def test_guest_and_invalid_token_reject_explicit_full_before_materialization(
    invalid: bool,
) -> None:
    ctx = RpcContext(conn_id="lan", principal=_guest_principal(invalid=invalid))

    with pytest.raises(RpcHandlerError) as raised:
        _trusted_run_mode_hint(ctx, {"runMode": "full"})

    assert raised.value.code == "HOST_CAPABILITY_REQUIRED"


def test_guest_route_uses_ephemeral_workspace_and_scrubbed_environment(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "opensquilla-state"
    configured_workspace = tmp_path / "configured-workspace"
    configured_workspace.mkdir()
    profile = GuestProfileFactory.create("turn", state_dir=state_dir)
    envelope = build_web_route_envelope(session_key="agent:main:web:guest")
    envelope.metadata["guest_safe"] = True
    envelope.metadata["guest_environment"] = dict(profile.environment)
    envelope.metadata["run_mode"] = "safe"
    envelope.metadata["sandbox_run_context"] = profile.run_context().to_origin_payload()

    context = tool_context_from_envelope(
        envelope,
        is_owner=False,
        workspace_dir=str(configured_workspace),
    )

    assert context.guest_safe is True
    assert context.run_mode == "safe"
    assert Path(context.workspace_dir or "").resolve() == profile.workspace.resolve()
    assert profile.workspace != configured_workspace.resolve()
    assert profile.managed_root == tmp_path / "opensquilla-state-guest-workspaces"
    assert profile.workspace == profile.root / "workspace"
    assert profile.home == profile.root / "home"
    assert profile.temp == profile.root / "tmp"
    assert context.environment == profile.environment
    scratch_root = profile.root
    profile.cleanup()
    assert configured_workspace.is_dir()
    assert not scratch_root.exists()


def test_guest_shell_environment_never_inherits_host_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-secret")
    state_dir = tmp_path / "opensquilla-state"
    profile = GuestProfileFactory.create("turn", state_dir=state_dir)
    token = current_tool_context.set(
        ToolContext(
            guest_safe=True,
            workspace_dir=str(profile.workspace),
            environment=profile.environment,
        )
    )
    try:
        environment = _base_shell_environment()
    finally:
        current_tool_context.reset(token)
        profile.cleanup()

    assert "AWS_SECRET_ACCESS_KEY" not in environment


@pytest.mark.parametrize("invalid", [False, True])
def test_missing_and_invalid_token_materialize_the_same_guest_boundary(
    invalid: bool,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "opensquilla-state"
    profile = _guest_profile_for_principal(
        _guest_principal(invalid=invalid),
        "turn",
        state_dir=state_dir,
    )

    assert profile is not None
    assert profile.run_context().run_mode.value == "safe"
    assert profile.managed_root == tmp_path / "opensquilla-state-guest-workspaces"
    assert profile.workspace == profile.root / "workspace"
    assert profile.home == profile.root / "home"
    assert profile.temp == profile.root / "tmp"
    assert profile.host_home_mounted is False
    profile.cleanup()


def test_guest_route_envelope_receives_the_hard_tool_allowlist(tmp_path: Path) -> None:
    profile = _guest_profile_for_principal(
        _guest_principal(),
        "turn",
        state_dir=tmp_path / "state",
    )
    assert profile is not None
    envelope = build_web_route_envelope(session_key="agent:main:webchat:guest")
    envelope.metadata.update(
        {
            "guest_safe": True,
            "guest_environment": dict(profile.environment),
            "run_mode": "safe",
            "sandbox_run_context": profile.run_context().to_origin_payload(),
        }
    )
    try:
        context = tool_context_from_envelope(envelope, is_owner=False)
    finally:
        profile.cleanup()

    assert context.allowed_tools == set(guest_safe_tool_allowlist())
    assert "sessions_send" not in context.allowed_tools
    assert "sessions_spawn" not in context.allowed_tools
    assert "skill_view" not in context.allowed_tools
    assert "memory_search" not in context.allowed_tools
    assert "exec_command" not in context.allowed_tools


@pytest.mark.parametrize(
    "principal",
    [
        Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
            auth_state="authenticated",
        ),
        Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
            auth_state="authenticated",
        ),
    ],
)
def test_authenticated_token_and_owner_do_not_materialize_guest_profile(
    principal: Principal,
    tmp_path: Path,
) -> None:
    assert (
        _guest_profile_for_principal(
            principal,
            "turn",
            state_dir=tmp_path / "state",
        )
        is None
    )


def test_guest_profile_projects_managed_rw_and_bundled_runtime_roots(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    python_root = tmp_path / "runtime" / "python"
    node_root = tmp_path / "runtime" / "node"
    git_bash_root = tmp_path / "runtime" / "git-bash"
    for root in (python_root, node_root, git_bash_root):
        (root / "bin").mkdir(parents=True)

    profile = GuestProfileFactory.create(
        "turn",
        state_dir=state_dir,
        runtime_roots=(python_root, node_root, git_bash_root),
        runtime_path=(python_root / "bin", node_root / "bin", git_bash_root / "bin"),
    )
    mounts = {mount.path: mount.access for mount in profile.mounts}
    run_mounts = {Path(mount.path): mount.access for mount in profile.run_context().mounts}

    assert mounts[profile.workspace] == "rw"
    assert mounts[profile.home] == "rw"
    assert mounts[profile.temp] == "rw"
    assert mounts[python_root] == "ro"
    assert mounts[node_root] == "ro"
    assert mounts[git_bash_root] == "ro"
    assert run_mounts[profile.home] == "rw"
    assert run_mounts[profile.temp] == "rw"
    assert run_mounts[python_root] == "ro"
    assert profile.environment["PATH"].split(os.pathsep) == [
        str(python_root / "bin"),
        str(node_root / "bin"),
        str(git_bash_root / "bin"),
    ]
    profile.cleanup()


def test_guest_profile_uses_only_managed_runtime_pack_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.runtime_packs as runtime_packs

    state_dir = tmp_path / "state"
    python_root = tmp_path / "runtime-packs" / "python"
    python_bin = python_root / "bin"
    python_bin.mkdir(parents=True)

    class _Service:
        management_supported = True

    class _Resolver:
        def __init__(self, service) -> None:
            assert service.management_supported is True

        def runtime_roots(self, policy):
            assert policy.python is True
            return (python_root,)

        def managed_path(self, policy):
            assert policy.python is True
            return (python_bin,)

    monkeypatch.setattr(runtime_packs, "get_runtime_pack_service", lambda _state: _Service())
    monkeypatch.setattr(runtime_packs, "RuntimePackResolver", _Resolver)

    profile = _guest_profile_for_principal(
        _guest_principal(),
        "turn",
        state_dir=state_dir,
    )

    assert profile is not None
    assert profile.environment["PATH"] == str(python_bin)
    assert any(mount.path == python_root and mount.access == "ro" for mount in profile.mounts)
    profile.cleanup()


@pytest.mark.skipif(os.name != "nt", reason="Windows guest runtime policy")
def test_windows_guest_profile_does_not_project_process_runtimes(
    tmp_path: Path,
) -> None:
    profile = _guest_profile_for_principal(
        _guest_principal(),
        "turn",
        state_dir=tmp_path / "state",
    )

    assert profile is not None
    assert all(mount.kind != "bundled-runtime" for mount in profile.mounts)
    assert profile.environment["PATH"] == ""
    profile.cleanup()


def test_guest_cleanup_rejects_alias_and_outside_factory_shape(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    profile = GuestProfileFactory.create("turn", state_dir=state_dir)
    alias = tmp_path / "guest-alias"
    _directory_alias(alias, profile.root)
    outside = tmp_path / profile.root.name
    outside.mkdir()
    forged = profile.managed_root / "opensquilla-guest-forged-12345678"
    forged.mkdir()

    assert cleanup_guest_profile_root(alias, managed_root=profile.managed_root) is False
    assert cleanup_guest_profile_root(outside, managed_root=profile.managed_root) is False
    assert cleanup_guest_profile_root(forged, managed_root=profile.managed_root) is False
    assert profile.root.exists()
    assert outside.exists()
    assert forged.exists()
    assert profile.cleanup() is None
    assert not profile.root.exists()
    assert outside.exists()


def test_guest_factory_rejects_managed_root_alias_to_authority(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    managed_root = tmp_path / "state-guest-workspaces"
    _directory_alias(managed_root, state_dir)

    with pytest.raises(Exception, match="guest workspace root is retargeted"):
        GuestProfileFactory.create("turn", state_dir=state_dir)
