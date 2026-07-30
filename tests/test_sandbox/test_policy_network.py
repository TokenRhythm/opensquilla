from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.policy import LevelHints, build_policy
from opensquilla.sandbox.types import MountSpec, NetworkMode, SecurityLevel


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="host root read-only mount is implemented by the Linux backend first",
)
def test_linux_policy_mounts_root_readonly_before_workspace(tmp_path: Path) -> None:
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(),
    )

    assert policy.mounts[0] == MountSpec(
        host_path=Path("/"),
        sandbox_path=Path("/"),
        mode="ro",
        required=True,
    )
    assert policy.mounts[1].host_path == tmp_path
    assert policy.mounts[1].mode == "rw"


def test_standard_network_http_uses_managed_allowlist_by_default(tmp_path: Path) -> None:
    policy = build_policy(
        SecurityLevel.STANDARD,
        "network.http",
        tmp_path,
        SandboxSettings(),
        trusted=True,
    )

    assert policy.network is NetworkMode.PROXY_ALLOWLIST


def test_standard_shell_and_code_exec_keep_network_none(tmp_path: Path) -> None:
    settings = SandboxSettings()

    shell_policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
    )
    code_policy = build_policy(
        SecurityLevel.STANDARD,
        "code.exec",
        tmp_path,
        settings,
        trusted=True,
    )

    assert shell_policy.network is NetworkMode.NONE
    assert code_policy.network is NetworkMode.NONE


def test_standard_shell_and_code_exec_with_network_hint_use_proxy(
    tmp_path: Path,
) -> None:
    settings = SandboxSettings(network_default="proxy_allowlist")
    hints = LevelHints(needs_network=True)

    shell_policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
        hints=hints,
    )
    code_policy = build_policy(
        SecurityLevel.STANDARD,
        "code.exec",
        tmp_path,
        settings,
        trusted=True,
        hints=hints,
    )

    assert shell_policy.network is NetworkMode.PROXY_ALLOWLIST
    assert code_policy.network is NetworkMode.PROXY_ALLOWLIST


def test_network_default_none_blocks_hinted_shell_network(
    tmp_path: Path,
) -> None:
    settings = SandboxSettings(network_default="none")
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        settings,
        trusted=True,
        hints=LevelHints(needs_network=True),
    )

    assert policy.network is NetworkMode.NONE


def test_network_hint_does_not_widen_non_network_non_exec_actions(
    tmp_path: Path,
) -> None:
    policy = build_policy(
        SecurityLevel.STANDARD,
        "fs.read",
        tmp_path,
        SandboxSettings(network_default="proxy_allowlist"),
        trusted=True,
        hints=LevelHints(needs_network=True),
    )

    assert policy.network is NetworkMode.NONE


def test_shell_exec_policy_allows_meta_skill_workspace_env(tmp_path: Path) -> None:
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(),
        trusted=True,
    )

    assert "WORKSPACE_DIR" in policy.env_allowlist
    assert "PROJECT_ROOT" in policy.env_allowlist
    assert "PSModulePath" in policy.env_allowlist
    assert "PATHEXT" in policy.env_allowlist


def test_network_default_proxy_allowlist_uses_proxy_for_network_actions(
    tmp_path: Path,
) -> None:
    settings = SandboxSettings(network_default="proxy_allowlist")
    policy = build_policy(
        SecurityLevel.STANDARD,
        "network.http",
        tmp_path,
        settings,
        trusted=True,
    )
    assert policy.network is NetworkMode.PROXY_ALLOWLIST


def test_network_default_none_blocks_network_actions(tmp_path: Path) -> None:
    settings = SandboxSettings(network_default="none")
    policy = build_policy(
        SecurityLevel.STANDARD,
        "network.http",
        tmp_path,
        settings,
        trusted=True,
    )

    assert policy.network is NetworkMode.NONE
