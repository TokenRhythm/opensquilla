from __future__ import annotations

import inspect
import io
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_windows_acl_internal_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from opensquilla.sandbox.backend import windows_default_runner as mod

    monkeypatch.setattr(
        mod, "_default_allow_acl_state_path", lambda: tmp_path / "allow_acl_state.json"
    )
    monkeypatch.setattr(mod, "_default_execution_lease_path", lambda: tmp_path / "execution.lock")


def test_parse_payload_accepts_valid_windows_default_payload(tmp_path) -> None:
    from opensquilla.sandbox.backend.windows_default_runner import _parse_payload

    payload = {
        "backend": "windows_default",
        "argv": ["python", "-c", "print('ok')"],
        "cwd": str(tmp_path),
        "env": {"TEMP": str(tmp_path / ".opensquilla-cache" / "temp")},
        "policy": {"network": "none", "mounts": [], "workspace_rw": True},
        "runMode": "trusted",
        "timeout": 5,
    }

    parsed = _parse_payload([json.dumps(payload)])

    assert parsed.argv == ("python", "-c", "print('ok')")
    assert parsed.cwd == tmp_path
    assert parsed.run_mode == "trusted"


def test_parse_payload_decodes_stdin_base64(tmp_path) -> None:
    from opensquilla.sandbox.backend.windows_default_runner import _parse_payload

    payload = {
        "backend": "windows_default",
        "argv": ["cmd", "/c", "more"],
        "cwd": str(tmp_path),
        "env": {},
        "policy": {
            "network": "none",
            "mounts": [],
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
        },
        "runMode": "trusted",
        "timeout": 5,
        "stdinBase64": "YWJjMTIz",
    }

    parsed = _parse_payload([json.dumps(payload)])

    assert parsed.stdin == b"abc123"


def test_parse_payload_accepts_payload_from_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = {
        "backend": "windows_default",
        "argv": ["cmd", "/c", "echo ok"],
        "cwd": str(tmp_path),
        "env": {},
        "policy": {
            "network": "none",
            "mounts": [],
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
        },
        "runMode": "trusted",
        "timeout": 5,
        "offlineChild": True,
    }
    monkeypatch.setenv(mod.OFFLINE_PAYLOAD_ENV, json.dumps(payload))

    parsed = mod._parse_payload(["--payload-env"])

    assert parsed.argv == ("cmd", "/c", "echo ok")
    assert parsed.offline_child is True


def test_parse_payload_accepts_payload_from_stdin(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = {
        "backend": "windows_default",
        "argv": ["cmd", "/c", "echo ok"],
        "cwd": str(tmp_path),
        "env": {},
        "policy": {
            "network": "none",
            "mounts": [],
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
        },
        "runMode": "trusted",
        "timeout": 5,
        "offlineChild": True,
    }
    stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode("utf-8")))
    monkeypatch.setattr(mod.sys, "stdin", stdin)

    parsed = mod._parse_payload(["--payload-stdin"])

    assert parsed.argv == ("cmd", "/c", "echo ok")
    assert parsed.offline_child is True


def test_parse_payload_rejects_wrong_backend(tmp_path) -> None:
    from opensquilla.sandbox.backend.windows_default_runner import _parse_payload

    payload = {
        "backend": "not_windows_default",
        "argv": ["cmd"],
        "cwd": str(tmp_path),
        "env": {},
        "policy": {"network": "none", "mounts": []},
        "runMode": "trusted",
        "timeout": 5,
    }

    with pytest.raises(SystemExit, match="expected backend windows_default"):
        _parse_payload([json.dumps(payload)])


def test_runner_rejects_proxy_allowlist_without_proxy_endpoint(tmp_path) -> None:
    from opensquilla.sandbox.backend.windows_default_runner import (
        _parse_payload,
        _validate_policy_is_enforceable,
    )

    payload = {
        "backend": "windows_default",
        "argv": ["cmd"],
        "cwd": str(tmp_path),
        "env": {},
        "policy": {"network": "proxy_allowlist", "mounts": []},
        "runMode": "trusted",
        "timeout": 5,
    }

    parsed = _parse_payload([json.dumps(payload)])

    with pytest.raises(SystemExit, match="requires network_proxy"):
        _validate_policy_is_enforceable(parsed.policy)


def test_runner_accepts_proxy_allowlist_with_proxy_endpoint(tmp_path) -> None:
    from opensquilla.sandbox.backend.windows_default_runner import (
        _parse_payload,
        _validate_policy_is_enforceable,
    )

    payload = {
        "backend": "windows_default",
        "argv": ["cmd"],
        "cwd": str(tmp_path),
        "env": {
            "HTTP_PROXY": "http://127.0.0.1:48123",
            "HTTPS_PROXY": "http://127.0.0.1:48123",
            "NO_PROXY": "",
        },
        "policy": {
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
            "mounts": [],
        },
        "runMode": "trusted",
        "timeout": 5,
    }

    parsed = _parse_payload([json.dumps(payload)])

    _validate_policy_is_enforceable(parsed.policy)


def test_runner_rejects_proxy_allowlist_without_windows_network_boundary(tmp_path) -> None:
    from opensquilla.sandbox.backend.windows_default_runner import (
        _parse_payload,
        _validate_policy_is_enforceable,
    )

    payload = {
        "backend": "windows_default",
        "argv": ["cmd"],
        "cwd": str(tmp_path),
        "env": {},
        "policy": {
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "mounts": [],
        },
        "runMode": "trusted",
        "timeout": 5,
    }

    parsed = _parse_payload([json.dumps(payload)])

    with pytest.raises(SystemExit, match="windowsNetworkBoundary"):
        _validate_policy_is_enforceable(parsed.policy)


def test_runner_accepts_proxy_allowlist_with_matching_network_boundary(tmp_path) -> None:
    from opensquilla.sandbox.backend.windows_default_runner import (
        _parse_payload,
        _validate_policy_is_enforceable,
    )

    payload = {
        "backend": "windows_default",
        "argv": ["cmd"],
        "cwd": str(tmp_path),
        "env": {},
        "policy": {
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
            "mounts": [],
        },
        "runMode": "trusted",
        "timeout": 5,
    }

    parsed = _parse_payload([json.dumps(payload)])

    _validate_policy_is_enforceable(parsed.policy)


@pytest.mark.parametrize(
    "argv",
    [
        ("ping.exe", "-n", "1", "1.1.1.1"),
        ("tracert.exe", "-d", "1.1.1.1"),
        ("pathping.exe", "-n", "1.1.1.1"),
        ("powershell.exe", "-NoProfile", "-Command", "ping -n 1 1.1.1.1"),
        ("powershell.exe", "-NoProfile", "-Command", "Test-Connection -Count 1 1.1.1.1"),
        (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[System.Net.NetworkInformation.Ping]::new().Send('1.1.1.1')",
        ),
        ("cmd.exe", "/c", "ping -n 1 1.1.1.1"),
    ],
)
def test_proxy_allowlist_blocks_icmp_diagnostics(argv) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    assert mod._proxy_allowlist_icmp_block_reason(argv) is not None


def test_proxy_allowlist_blocks_shell_host_wrapped_icmp_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod
    from opensquilla.tools.builtin import shell

    runtime = SimpleNamespace(backend=SimpleNamespace(name="windows_default"))
    monkeypatch.setattr(
        shell,
        "_trusted_windows_powershell_path",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )

    argv = shell._sandbox_shell_backend_argv(
        "ping -n 1 1.1.1.1",
        runtime,
        cwd=tmp_path,
    )

    assert mod._proxy_allowlist_icmp_block_reason(argv) is not None


def test_proxy_allowlist_icmp_guard_allows_regular_http_commands() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    assert (
        mod._proxy_allowlist_icmp_block_reason(
            ("powershell.exe", "-NoProfile", "-Command", "Invoke-WebRequest https://example.com")
        )
        is None
    )


def test_proxy_allowlist_icmp_guard_is_enforced_before_acl_refresh(tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("ping.exe", "-n", "1", "1.1.1.1"),
        cwd=tmp_path,
        env={},
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
        },
        run_mode="trusted",
        timeout=5,
    )

    with pytest.raises(SystemExit, match="blocks ICMP"):
        mod._run_windows_default(payload)


def test_runner_applies_acl_refresh_before_process_launch(tmp_path, monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    calls = []
    payload = mod.HelperPayload(
        argv=("cmd", "/c", "echo ok"),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "mounts": [],
            "windowsAclPlan": {
                "autoGrants": [
                    {
                        "path": str(tmp_path),
                        "access": "RWX",
                        "capabilitySid": "S-1-5-21-100-101-102-103",
                    }
                ],
                "capabilitySids": ["S-1-5-21-100-101-102-103"],
            },
        },
        run_mode="trusted",
        timeout=5,
    )

    payload = replace(
        payload,
        offline_child=True,
        policy={
            **payload.policy,
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-test",
                "offlineUsername": "sandbox",
                "protectedPassword": "protected",
            },
        },
    )
    monkeypatch.setattr(mod, "_apply_acl_refresh", lambda plan: calls.append(("acl", plan)))
    monkeypatch.setattr(
        mod,
        "_run_restricted_process_native",
        lambda payload, sids: calls.append(("run", sids)) or 0,
    )

    assert mod._run_windows_default(payload) == 0
    assert calls == [("run", ("S-1-5-21-100-101-102-103",))]


def test_parent_execution_lease_covers_acl_and_process_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from opensquilla.sandbox.backend import windows_default_runner as mod

    events: list[str] = []
    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {},
        },
        run_mode="trusted",
        timeout=5,
    )

    @contextmanager
    def lease():
        events.append("lease-enter")
        yield
        events.append("lease-exit")

    monkeypatch.setattr(mod, "_windows_acl_execution_lease", lease)
    monkeypatch.setattr(
        mod,
        "_resolve_offline_launch_credentials",
        lambda _p: mod.OfflineLaunchCredentials("S", "u", "p"),
    )
    monkeypatch.setattr(
        mod, "_prepare_deny_acl_targets", lambda *_a, **_k: events.append("prepare")
    )
    monkeypatch.setattr(mod, "_apply_acl_refresh", lambda *_a, **_k: events.append("acl"))
    monkeypatch.setattr(
        mod, "_run_payload_as_offline_identity", lambda *_a, **_k: events.append("process") or 0
    )

    assert mod._run_windows_default(payload) == 0
    assert events == ["lease-enter", "prepare", "acl", "process", "lease-exit"]


def test_offline_child_does_not_reacquire_execution_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-test",
                "offlineUsername": "sandbox",
                "protectedPassword": "protected",
            },
        },
        run_mode="trusted",
        timeout=5,
        offline_child=True,
    )
    monkeypatch.setattr(
        mod, "_windows_acl_execution_lease", lambda: pytest.fail("lease reacquired")
    )
    monkeypatch.setattr(mod, "_prepare_deny_acl_targets", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_run_restricted_process_native", lambda *_a: 0)
    assert mod._run_windows_default(payload) == 0


def test_cross_process_execution_lease_serializes_concurrent_runs(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    lock = tmp_path / "execution.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with mod._cross_process_file_lock(lock):
            first_entered.set()
            assert release_first.wait(2)

    def second() -> None:
        assert first_entered.wait(2)
        with mod._cross_process_file_lock(lock):
            second_entered.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    two.start()
    assert first_entered.wait(2)
    assert not second_entered.wait(0.1)
    release_first.set()
    one.join(2)
    two.join(2)
    assert second_entered.is_set()


def test_offline_parent_reconciles_capability_denies_before_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    events: list[tuple[str, object]] = []
    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "windowsNetworkBoundary": {},
            "windowsAclPlan": {
                "autoGrants": [],
                "capabilitySids": ["S-cap"],
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    monkeypatch.setattr(
        mod,
        "_resolve_offline_launch_credentials",
        lambda _p: mod.OfflineLaunchCredentials("S-off", "u", "p"),
    )
    monkeypatch.setattr(mod, "_prepare_deny_acl_targets", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mod,
        "_apply_acl_refresh",
        lambda plan: events.append(("reconcile", tuple(plan["capabilitySids"]))),
    )
    monkeypatch.setattr(
        mod,
        "_run_payload_as_offline_identity",
        lambda *_a, **_k: events.append(("transition", None)) or 0,
    )

    assert mod._run_windows_default(payload) == 0
    assert events == [("reconcile", ("S-cap",)), ("transition", None)]


def test_grant_path_to_sid_uses_native_acl_writer(tmp_path, monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    calls = []

    def fail_subprocess(*args, **kwargs):  # pragma: no cover - only reached on regression
        raise AssertionError("icacls must not be used for random restricting SIDs")

    monkeypatch.setattr(mod.subprocess, "run", fail_subprocess)
    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid_native",
        lambda path, access, sid: calls.append((path, access, sid)),
    )

    mod._grant_path_to_sid(tmp_path, "RWX", "S-1-5-21-100-101-102-103")

    assert calls == [(tmp_path, "RWX", "S-1-5-21-100-101-102-103")]


def test_deny_write_path_to_sid_uses_native_acl_writer(tmp_path, monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    calls = []

    def fail_subprocess(*args, **kwargs):  # pragma: no cover - only reached on regression
        raise AssertionError("icacls must not be used for random restricting SIDs")

    monkeypatch.setattr(mod.subprocess, "run", fail_subprocess)
    monkeypatch.setattr(
        mod,
        "_deny_path_to_sid_native",
        lambda path, sid, *, mask: calls.append((path, sid, mask)),
    )

    mod._deny_write_path_to_sid(tmp_path, "S-1-5-21-100-101-102-103")

    assert calls == [(tmp_path, "S-1-5-21-100-101-102-103", mod.FILE_WRITE_DENY_MASK)]


def test_deny_read_path_to_sid_uses_shared_native_acl_writer(tmp_path, monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    calls = []
    monkeypatch.setattr(
        mod,
        "_deny_path_to_sid_native",
        lambda path, sid, *, mask: calls.append((path, sid, mask)),
        raising=False,
    )

    mod._deny_read_path_to_sid(tmp_path, "S-1-5-21-read")

    assert calls == [(tmp_path, "S-1-5-21-read", mod.FILE_READ_DENY_MASK)]


def test_windows_acl_plan_rejects_invalid_deny_read_paths() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    with pytest.raises(SystemExit, match="denyReadPaths must be a string list"):
        mod._windows_acl_plan(
            {
                "windowsAclPlan": {
                    "autoGrants": [],
                    "capabilitySids": [],
                    "denyReadPaths": [1],
                }
            }
        )


def test_windows_acl_plan_rejects_untrusted_deny_acl_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    trusted = tmp_path / "trusted" / "deny_acl_state.json"
    monkeypatch.setattr(mod, "_default_deny_acl_state_path", lambda: trusted)

    with pytest.raises(SystemExit, match="denyAclStatePath is not trusted"):
        mod._windows_acl_plan(
            {
                "windowsAclPlan": {
                    "autoGrants": [],
                    "capabilitySids": [],
                    "denyAclStatePath": str(tmp_path / "attacker-controlled.json"),
                }
            }
        )


def test_deny_masks_require_all_requested_bits() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    assert not mod._ace_mask_covers(mod.FILE_WRITE_DENY_MASK, mod.FILE_READ_DENY_MASK)
    assert mod._ace_mask_covers(mod.FILE_READ_DENY_MASK, mod.FILE_READ_DENY_MASK)


def test_acl_refresh_skips_missing_expansion_grants(tmp_path, monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    existing = tmp_path / "existing"
    missing = tmp_path / "deleted-probe.txt"
    existing.mkdir()
    calls = []

    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: calls.append((path, access, sid)),
    )

    mod._apply_acl_refresh(
        {
            "autoGrants": [
                {
                    "path": str(missing),
                    "access": "RWX",
                    "kind": "expansion",
                    "capabilitySid": "S-1-5-21-100-101-102-103",
                },
                {
                    "path": str(existing),
                    "access": "RWX",
                    "kind": "required",
                    "capabilitySid": "S-1-5-21-100-101-102-104",
                },
            ]
        }
    )

    assert calls == [(existing, "RWX", "S-1-5-21-100-101-102-104")]


def test_acl_refresh_grants_current_user_normal_access_when_requested(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    monkeypatch.setattr(
        mod,
        "_current_token_user_sid_string",
        lambda: "S-1-5-21-user",
    )
    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: calls.append((path, access, sid)),
    )

    mod._apply_acl_refresh(
        {
            "autoGrants": [
                {
                    "path": str(workspace),
                    "access": "RWX",
                    "kind": "required",
                    "capabilitySid": "S-1-5-21-capability",
                }
            ],
            "capabilitySids": ["S-1-5-21-capability"],
            "grantCurrentUserAccess": True,
        }
    )

    assert calls == [
        (workspace, "RWX", "S-1-5-21-capability"),
        (workspace, "HOST_RWX", "S-1-5-21-user"),
    ]


def test_acl_refresh_skips_missing_policy_grants(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    missing = tmp_path / "deleted-probe.txt"
    calls = []

    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: calls.append((path, access, sid)),
    )

    mod._apply_acl_refresh(
        {
            "autoGrants": [
                {
                    "path": str(missing),
                    "access": "RWX",
                    "kind": "policy",
                    "capabilitySid": "S-1-5-21-capability",
                }
            ],
            "capabilitySids": ["S-1-5-21-capability"],
        }
    )

    assert calls == []


def test_acl_refresh_does_not_apply_deny_write_to_unrelated_capability_sids(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    runtime = tmp_path / "runtime" / "Scripts"
    workspace = tmp_path / "workspace"
    runtime_rx = tmp_path / "runtime-rx"
    runtime.mkdir(parents=True)
    workspace.mkdir()
    runtime_rx.mkdir()
    grants = []
    denies = []

    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: grants.append((path, access, sid)),
    )
    monkeypatch.setattr(
        mod,
        "_deny_write_path_to_sid",
        lambda path, sid: denies.append((path, sid)),
        raising=False,
    )

    mod._apply_acl_refresh(
        {
            "autoGrants": [
                {
                    "path": str(workspace),
                    "access": "RWX",
                    "kind": "required",
                    "capabilitySid": "S-1-5-21-100-101-102-103",
                },
                {
                    "path": str(runtime_rx),
                    "access": "RX",
                    "kind": "required",
                    "capabilitySid": "S-1-5-21-100-101-102-104",
                },
            ],
            "denyWritePaths": [str(runtime)],
            "capabilitySids": [
                "S-1-5-21-100-101-102-103",
                "S-1-5-21-100-101-102-104",
            ],
        }
    )

    assert grants == [
        (workspace, "RWX", "S-1-5-21-100-101-102-103"),
        (runtime_rx, "RX", "S-1-5-21-100-101-102-104"),
    ]
    assert denies == []


def test_apply_acl_refresh_never_applies_deny_read_to_capability_sid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    readable = tmp_path / "readable"
    denied = readable / "secret"
    denied.mkdir(parents=True)
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(mod, "_grant_path_to_sid", lambda *args: None)
    monkeypatch.setattr(
        mod,
        "_deny_read_path_to_sid",
        lambda path, sid: calls.append((path, sid)),
        raising=False,
    )

    mod._apply_acl_refresh(
        {
            "autoGrants": [
                {
                    "path": str(readable),
                    "access": "RX",
                    "kind": "policy",
                    "capabilitySid": "S-1-test",
                }
            ],
            "capabilitySids": ["S-1-test"],
            "denyWritePaths": [],
            "denyReadPaths": [str(denied)],
        }
    )

    assert calls == []


def test_runner_fails_before_acl_mutation_when_deny_read_has_no_offline_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    denied = tmp_path / "secret"
    denied.mkdir()
    calls: list[str] = []
    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "windowsAclPlan": {
                "autoGrants": [],
                "capabilitySids": [],
                "denyWritePaths": [],
                "denyReadPaths": [str(denied)],
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    monkeypatch.setattr(mod, "_apply_acl_refresh", lambda *_args, **_kwargs: calls.append("acl"))
    monkeypatch.setattr(
        mod,
        "_run_restricted_process_native",
        lambda *_args: calls.append("run") or 0,
    )

    with pytest.raises(OSError, match="windowsNetworkBoundary missing"):
        mod._run_windows_default(payload)

    assert calls == []


def test_acl_refresh_materializes_missing_deny_write_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    missing = tmp_path / "missing" / "nested"
    observed: list[bool] = []
    monkeypatch.setattr(mod, "_grant_path_to_sid", lambda *args: None)
    monkeypatch.setattr(
        mod,
        "_sync_deny_acl_state",
        lambda _state, _sid, desired: observed.append(all(path.exists() for path in desired)),
        raising=False,
    )

    mod._apply_acl_refresh(
        {
            "autoGrants": [
                {
                    "path": str(tmp_path),
                    "access": "RWX",
                    "kind": "required",
                    "capabilitySid": "S-1-write",
                }
            ],
            "capabilitySids": ["S-1-write"],
            "denyWritePaths": [str(missing)],
            "denyReadPaths": [],
            "denyAclStatePath": str(tmp_path / "state.json"),
        }
    )

    assert missing.is_dir()
    assert observed == [True]


def test_deny_write_capability_sids_prefer_overlapping_write_roots(tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    protected = nested / ".codex"

    assert mod._deny_write_capability_sids_for_path(
        {
            "autoGrants": [
                {
                    "path": str(workspace),
                    "access": "RWX",
                    "capabilitySid": "workspace-sid",
                },
                {
                    "path": str(nested),
                    "access": "RWX",
                    "capabilitySid": "nested-sid",
                },
                {
                    "path": str(tmp_path / "runtime"),
                    "access": "RX",
                    "capabilitySid": "runtime-sid",
                },
            ],
            "capabilitySids": ["workspace-sid", "nested-sid", "runtime-sid"],
        },
        protected,
    ) == ("workspace-sid", "nested-sid")


def test_deny_write_capability_sids_preserve_writable_grandchild_reopen(tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    workspace = tmp_path / "workspace"
    readonly = workspace / ".git"
    writable_reopen = readonly / "objects"

    assert mod._deny_write_capability_sids_for_path(
        {
            "autoGrants": [
                {
                    "path": str(workspace),
                    "access": "RWX",
                    "capabilitySid": "workspace-sid",
                },
                {
                    "path": str(writable_reopen),
                    "access": "RWX",
                    "capabilitySid": "reopen-sid",
                },
            ],
            "capabilitySids": ["workspace-sid", "reopen-sid"],
        },
        readonly,
    ) == ("workspace-sid",)


def test_grant_acl_plan_to_sid_does_not_apply_deny_write_paths_to_offline_user(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    grants = []
    denies = []

    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: grants.append((path, access, sid)),
    )
    monkeypatch.setattr(
        mod,
        "_deny_write_path_to_sid",
        lambda path, sid: denies.append((path, sid)),
    )

    mod._grant_acl_plan_to_sid(
        {
            "autoGrants": [
                {
                    "path": str(workspace),
                    "access": "RWX",
                    "kind": "required",
                    "capabilitySid": "S-1-15-3-100-200-300",
                }
            ],
            "denyWritePaths": [str(runtime)],
            "capabilitySids": ["S-1-15-3-100-200-300"],
        },
        "S-1-5-21-100-200-300-400",
    )

    assert grants == [(workspace, "RWX", "S-1-5-21-100-200-300-400")]
    assert denies == []


def test_offline_identity_launch_syncs_read_and_write_denies_to_actual_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    runtime_root = tmp_path / "runtime"
    readonly_mount = tmp_path / "readonly"
    denied_read = tmp_path / "secret"
    runtime_root.mkdir()
    readonly_mount.mkdir()
    denied_read.mkdir()
    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "windowsAclPlan": {
                "autoGrants": [],
                "denyWritePaths": [str(readonly_mount)],
                "denyReadPaths": [str(denied_read)],
                "capabilitySids": [],
            },
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    state_path = tmp_path / "state" / "deny_acl_state.json"
    payload.policy["windowsAclPlan"]["denyAclStatePath"] = str(state_path)
    syncs: list[tuple[Path, str, dict[Path, int]]] = []

    monkeypatch.setattr(mod, "_default_deny_acl_state_path", lambda: state_path)
    monkeypatch.setattr(mod, "_offline_helper_runtime_roots", lambda: (runtime_root,))
    monkeypatch.setattr(
        mod,
        "_sync_deny_acl_state",
        lambda state, sid, desired: syncs.append((state, sid, desired)),
        raising=False,
    )
    monkeypatch.setattr(mod, "_grant_path_to_sid", lambda *_args: None)
    import opensquilla.sandbox.backend.windows_default_identity as identity_mod

    monkeypatch.setattr(identity_mod, "unprotect_password", lambda _value: "plain")
    monkeypatch.setattr(
        mod,
        "_run_payload_as_offline_identity_native",
        lambda request, *, username, password: 9,
    )

    assert mod._run_payload_as_offline_identity(payload) == 9

    assert syncs == [
        (
            state_path,
            "S-1-5-21-100-200-300-400",
            {
                readonly_mount: mod.FILE_MUTATION_DENY_MASK,
                denied_read: mod.FILE_READ_DENY_MASK,
            },
        )
    ]


def test_offline_identity_launch_combines_read_and_write_denies_for_same_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    desired_entries: list[dict[Path, int]] = []

    monkeypatch.setattr(
        mod,
        "_sync_deny_acl_state",
        lambda _state, _sid, desired: desired_entries.append(desired),
        raising=False,
    )

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "windowsAclPlan": {
                "autoGrants": [],
                "denyWritePaths": [str(runtime)],
                "denyReadPaths": [str(runtime)],
                "capabilitySids": [],
            },
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )

    monkeypatch.setattr(mod, "_offline_helper_runtime_roots", lambda: (), raising=False)
    monkeypatch.setattr(mod, "_grant_path_to_sid", lambda *_args: None)
    import opensquilla.sandbox.backend.windows_default_identity as identity_mod

    monkeypatch.setattr(identity_mod, "unprotect_password", lambda _value: "plain")
    monkeypatch.setattr(
        mod,
        "_run_payload_as_offline_identity_native",
        lambda request, *, username, password: 9,
    )

    assert mod._run_payload_as_offline_identity(payload) == 9

    assert desired_entries == [{runtime: mod.FILE_MUTATION_DENY_MASK | mod.FILE_READ_DENY_MASK}]


def test_deny_acl_state_sync_materializes_desired_and_removes_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state_path = tmp_path / "state" / "deny_acl_state.json"
    stale = tmp_path / "stale"
    desired = tmp_path / "missing" / "nested"
    stale.mkdir()
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": {"S-1-test": [{"path": str(stale), "mask": mod.FILE_READ_DENY_MASK}]},
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, Path, int | None]] = []

    def deny(path: Path, sid: str, *, mask: int, label: str) -> None:
        assert desired.is_dir()
        calls.append((f"deny:{sid}:{label}", path, mask))

    monkeypatch.setattr(mod, "_deny_path_to_sid", deny)
    monkeypatch.setattr(
        mod,
        "_revoke_path_for_sid",
        lambda path, sid: calls.append((f"revoke:{sid}", path, None)),
    )

    mod._sync_deny_acl_state(
        state_path,
        "S-1-test",
        {desired: mod.FILE_MUTATION_DENY_MASK},
    )

    assert calls == [
        (
            "deny:S-1-test:desired-state",
            desired,
            mod.FILE_MUTATION_DENY_MASK,
        ),
        ("revoke:S-1-test", stale, None),
    ]
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["principals"]["S-1-test"] == [
        {"mask": mod.FILE_MUTATION_DENY_MASK, "path": str(desired)}
    ]


def test_deny_acl_state_sync_rolls_back_acl_when_state_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state_path = tmp_path / "state" / "deny_acl_state.json"
    previous = tmp_path / "previous"
    desired = tmp_path / "desired"
    previous.mkdir()
    desired.mkdir()
    state_path.parent.mkdir()
    original = {
        "version": 1,
        "principals": {"S-1-test": [{"path": str(previous), "mask": mod.FILE_READ_DENY_MASK}]},
    }
    state_path.write_text(json.dumps(original), encoding="utf-8")
    calls: list[tuple[str, Path, int | None]] = []
    monkeypatch.setattr(
        mod,
        "_deny_path_to_sid",
        lambda path, sid, *, mask, label: calls.append(("deny", path, mask)),
    )
    monkeypatch.setattr(
        mod,
        "_revoke_path_for_sid",
        lambda path, sid: calls.append(("revoke", path, None)),
    )
    original_write = mod._write_deny_acl_state

    def fail_state_write(path, payload):
        if path == state_path:
            raise OSError("disk full")
        original_write(path, payload)

    monkeypatch.setattr(mod, "_write_deny_acl_state", fail_state_write)

    with pytest.raises(SystemExit, match="state sync failed"):
        mod._sync_deny_acl_state(
            state_path,
            "S-1-test",
            {desired: mod.FILE_MUTATION_DENY_MASK},
        )

    assert calls == [
        ("deny", desired, mod.FILE_MUTATION_DENY_MASK),
        ("revoke", previous, None),
        ("revoke", desired, None),
        ("revoke", previous, None),
        ("deny", previous, mod.FILE_READ_DENY_MASK),
    ]
    assert json.loads(state_path.read_text(encoding="utf-8")) == original


def test_deny_acl_rollback_failure_leaves_taint_and_future_sync_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state = tmp_path / "deny_acl_state.json"
    path = tmp_path / "path"
    path.mkdir()
    monkeypatch.setattr(mod, "_deny_path_to_sid", lambda *_a, **_k: None)
    original_write = mod._write_deny_acl_state

    def fail_state_write(path_arg, payload):
        if path_arg == state:
            raise OSError("disk")
        original_write(path_arg, payload)

    monkeypatch.setattr(mod, "_write_deny_acl_state", fail_state_write)
    monkeypatch.setattr(
        mod, "_revoke_path_for_sid", lambda *_a: (_ for _ in ()).throw(OSError("restore"))
    )

    with pytest.raises(SystemExit, match="rollback_errors"):
        mod._sync_deny_acl_state(state, "S", {path: mod.FILE_READ_DENY_MASK})
    assert mod._acl_state_taint_path(state).exists()
    monkeypatch.setattr(mod, "_revoke_path_for_sid", lambda *_a: None)
    monkeypatch.setattr(mod, "_write_deny_acl_state", original_write)

    mod._sync_deny_acl_state(state, "S", {path: mod.FILE_READ_DENY_MASK})

    assert not mod._acl_state_taint_path(state).exists()


def test_handle_and_reader_guards_fail_deterministically() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    kernel = type("Kernel", (), {"SetHandleInformation": lambda *_a: 0})()
    with pytest.raises(OSError, match=r"SetHandleInformation\(stdout\)"):
        mod._clear_handle_inheritance(kernel, 1, "stdout")
    living = type("Thread", (), {"is_alive": lambda self: True})()
    with pytest.raises(OSError, match="pipe reader did not terminate"):
        mod._require_reader_threads_stopped((living,), label="restricted process")


def test_allow_acl_state_sync_revokes_stale_and_changes_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state = tmp_path / "allow_acl_state.json"
    stale = tmp_path / "stale"
    changed = tmp_path / "changed"
    new = tmp_path / "new"
    for path in (stale, changed, new):
        path.mkdir()
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": {
                    "S": [
                        {"path": str(stale), "access": "RX"},
                        {"path": str(changed), "access": "RWX"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, Path, str | None]] = []
    monkeypatch.setattr(
        mod, "_grant_path_to_sid", lambda path, access, sid: calls.append(("grant", path, access))
    )
    monkeypatch.setattr(
        mod, "_revoke_allow_path_for_sid", lambda path, sid: calls.append(("revoke", path, None))
    )

    mod._sync_allow_acl_state(state, "S", {changed: "RX", new: "RWX"})

    assert ("revoke", changed, None) in calls
    assert ("revoke", stale, None) in calls
    assert ("grant", changed, "RX") in calls
    assert ("grant", new, "RWX") in calls


def test_allow_acl_grants_child_before_revoking_inherited_parent_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state = tmp_path / "allow_acl_state.json"
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": {"S": [{"path": str(parent), "access": "RWX"}]},
            }
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: calls.append(("grant", path, access)),
    )
    monkeypatch.setattr(
        mod,
        "_revoke_allow_path_for_sid",
        lambda path, sid: calls.append(("revoke", path, None)),
    )

    mod._sync_allow_acl_state(state, "S", {child: "RX"})

    assert calls.index(("grant", child, "RX")) < calls.index(("revoke", parent, None))


@pytest.mark.parametrize("persisted_is_new", [False, True])
def test_allow_taint_recovers_crash_before_or_after_state_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persisted_is_new: bool
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state = tmp_path / "allow.json"
    previous = tmp_path / "previous"
    desired = tmp_path / "desired"
    previous.mkdir()
    desired.mkdir()
    persisted = desired if persisted_is_new else previous
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": {"S": [{"path": str(persisted), "access": "RX"}]},
            }
        ),
        encoding="utf-8",
    )
    mod._mark_acl_state_tainted(state, kind="allow", sid="S", paths=(previous, desired))
    calls = []
    monkeypatch.setattr(
        mod,
        "_revoke_allow_path_for_sid",
        lambda path, sid: calls.append(("revoke", path)),
    )
    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: calls.append(("grant", path)),
    )

    mod._sync_allow_acl_state(state, "S", {persisted: "RX"})

    assert ("revoke", previous) in calls
    assert ("revoke", desired) in calls
    assert ("grant", persisted) in calls
    assert not mod._acl_state_taint_path(state).exists()


@pytest.mark.parametrize("persisted_is_new", [False, True])
def test_deny_taint_recovers_crash_before_or_after_state_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persisted_is_new: bool
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state = tmp_path / "deny.json"
    previous = tmp_path / "previous"
    desired = tmp_path / "desired"
    previous.mkdir()
    desired.mkdir()
    persisted = desired if persisted_is_new else previous
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": {"S": [{"path": str(persisted), "mask": mod.FILE_READ_DENY_MASK}]},
            }
        ),
        encoding="utf-8",
    )
    mod._mark_acl_state_tainted(state, kind="deny", sid="S", paths=(previous, desired))
    calls = []
    monkeypatch.setattr(
        mod,
        "_revoke_path_for_sid",
        lambda path, sid: calls.append(("revoke", path)),
    )
    monkeypatch.setattr(
        mod,
        "_deny_path_to_sid",
        lambda path, sid, *, mask, label: calls.append(("deny", path)),
    )

    mod._sync_deny_acl_state(state, "S", {persisted: mod.FILE_READ_DENY_MASK})

    assert ("revoke", previous) in calls
    assert ("revoke", desired) in calls
    assert ("deny", persisted) in calls
    assert not mod._acl_state_taint_path(state).exists()


def test_taint_repair_failure_remains_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state = tmp_path / "allow.json"
    path = tmp_path / "path"
    path.mkdir()
    state.write_text(
        json.dumps({"version": 1, "principals": {"S": [{"path": str(path), "access": "RX"}]}}),
        encoding="utf-8",
    )
    mod._mark_acl_state_tainted(state, kind="allow", sid="S", paths=(path,))
    monkeypatch.setattr(
        mod,
        "_revoke_allow_path_for_sid",
        lambda *_a: (_ for _ in ()).throw(OSError("repair failed")),
    )

    with pytest.raises(SystemExit, match="remains fail-closed"):
        mod._sync_allow_acl_state(state, "S", {path: "RX"})

    assert mod._acl_state_taint_path(state).exists()


def test_sandbox_rwx_omits_delete_child_and_native_acl_ignores_inherited_allow() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    assert not mod.MANAGED_ALLOW_MASK & mod.FILE_DELETE_CHILD
    source = inspect.getsource(mod._grant_path_to_sid_native)
    status_source = inspect.getsource(mod._explicit_allow_ace_status)
    revoke_source = inspect.getsource(mod._revoke_path_for_sid_native)
    assert "allow_mask" in source and "| FILE_DELETE_CHILD" not in source
    assert "_explicit_allow_ace_status" in source
    assert "INHERITED_ACE_FLAG" in status_source
    assert "INHERITED_ACE" in revoke_source


def test_legacy_delete_child_cleanup_preserves_host_custom_full_control() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    write_owner = 0x00080000
    host_custom_mask = mod.MANAGED_ALLOW_MASK | mod.FILE_DELETE_CHILD | write_owner
    managed_legacy_mask = mod.MANAGED_ALLOW_MASK | mod.FILE_DELETE_CHILD

    assert mod._explicit_allow_ace_status(
        ace_mask=host_custom_mask,
        ace_flags=0,
        required_mask=mod.MANAGED_ALLOW_MASK,
        cleanup_legacy_delete_child=False,
    ) == (True, False)
    assert mod._explicit_allow_ace_status(
        ace_mask=managed_legacy_mask,
        ace_flags=0,
        required_mask=mod.MANAGED_ALLOW_MASK & ~mod.WRITE_DAC,
        cleanup_legacy_delete_child=True,
    ) == (False, True)


def test_managed_deny_mask_includes_read_and_write_denies() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    assert mod.MANAGED_DENY_MASK & mod.FILE_READ_DENY_MASK
    assert mod.MANAGED_DENY_MASK & mod.FILE_WRITE_DENY_MASK


def test_runner_rejects_missing_acl_plan(tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={"network": "none", "mounts": []},
        run_mode="trusted",
        timeout=5,
    )

    with pytest.raises(SystemExit, match="windowsAclPlan is required"):
        mod._run_windows_default(payload)


def test_restricted_token_flags_match_codex_legacy() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    assert mod.RESTRICTED_TOKEN_FLAGS == (
        mod.DISABLE_MAX_PRIVILEGE | mod.LUA_TOKEN | mod.WRITE_RESTRICTED
    )


def test_restricting_sid_specs_match_codex_legacy_base() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    sid_specs = mod._base_restricting_sid_specs()
    sid_values = {sid for sid, _label in sid_specs}

    assert "S-1-1-0" in sid_values
    assert "S-1-5-11" not in sid_values
    assert "S-1-5-32-545" not in sid_values
    assert "S-1-5-12" not in sid_values


def test_token_post_create_hooks_are_called(monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    calls = []

    monkeypatch.setattr(
        mod,
        "_set_token_default_dacl",
        lambda token, sids: calls.append(("default_dacl", token, tuple(sids))),
    )
    monkeypatch.setattr(
        mod,
        "_enable_token_privilege",
        lambda token, name: calls.append(("privilege", token, name)),
    )

    mod._finalize_restricted_token(
        token=123,
        dacl_sids=("logon", "everyone", "cap-a"),
    )

    assert calls == [
        ("default_dacl", 123, ("logon", "everyone", "cap-a")),
        ("privilege", 123, "SeChangeNotifyPrivilege"),
    ]


def test_child_stdin_writer_writes_payload_and_closes(monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    calls = []

    class FakeKernel32:
        def WriteFile(self, handle, data, size, written_ptr, overlapped):  # noqa: N802
            calls.append(("write", handle, bytes(data[:size])))
            written_ptr._obj.value = size
            return 1

        def CloseHandle(self, handle):  # noqa: N802
            calls.append(("close", handle))
            return 1

    mod._write_child_stdin(FakeKernel32(), 42, b"abc")

    assert calls == [("write", 42, b"abc"), ("close", 42)]


def test_native_launchers_assign_and_resume_before_starting_stdin_writer() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    for launcher in (
        mod._run_payload_as_offline_identity_native,
        mod._run_restricted_process_native_impl,
    ):
        source = inspect.getsource(launcher)
        assigned = source.index("job_assigned = True")
        resumed = source.index('raise win_error("ResumeThread")')
        writer_started = source.index("_start_child_stdin_writer")

        assert assigned < resumed < writer_started
        assert "_write_child_stdin" not in source


def test_child_stdin_writer_runs_concurrently_and_closes_once(monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    started = threading.Event()
    release = threading.Event()
    closed = []

    class FakeKernel32:
        def CloseHandle(self, handle):  # noqa: N802
            closed.append(handle)
            return 1

    def blocking_write(kernel32, handle, stdin, *, close_handle=True):
        assert close_handle is False
        started.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr(mod, "_write_child_stdin", blocking_write)

    thread, errors, close_writer = mod._start_child_stdin_writer(
        FakeKernel32(),
        42,
        b"large payload",
        on_error=lambda: None,
    )

    assert started.wait(timeout=1)
    assert thread.is_alive()
    release.set()
    mod._finish_child_io(
        writer_thread=thread,
        reader_threads=(),
        writer_errors=errors,
        close_writer=close_writer,
        label="test child",
        terminate=lambda: None,
    )
    close_writer()

    assert closed == [42]


def test_child_stdin_writer_failure_terminates_and_surfaces(monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    actions = []

    class FakeKernel32:
        def CloseHandle(self, handle):  # noqa: N802
            actions.append(("close", handle))
            return 1

    def failing_write(kernel32, handle, stdin, *, close_handle=True):
        raise OSError("broken stdin")

    monkeypatch.setattr(mod, "_write_child_stdin", failing_write)
    thread, errors, close_writer = mod._start_child_stdin_writer(
        FakeKernel32(),
        42,
        b"payload",
        on_error=lambda: actions.append(("terminate", "writer")),
    )

    with pytest.raises(OSError, match="stdin writer failed: broken stdin"):
        mod._finish_child_io(
            writer_thread=thread,
            reader_threads=(),
            writer_errors=errors,
            close_writer=close_writer,
            label="test child",
            terminate=lambda: actions.append(("terminate", "finish")),
        )

    assert actions == [
        ("terminate", "writer"),
        ("close", 42),
        ("terminate", "finish"),
    ]


def test_finish_child_io_cancels_and_rejoins_blocking_writer_and_readers() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    cancelled = False
    events = []

    class BlockingThread:
        def __init__(self, name):
            self.name = name
            self.joins = 0

        def join(self, timeout):
            self.joins += 1
            events.append(("join", self.name, timeout))

        def is_alive(self):
            return not cancelled

    writer = BlockingThread("writer")
    readers = (BlockingThread("stdout"), BlockingThread("stderr"))

    def cancel_io():
        nonlocal cancelled
        cancelled = True
        events.append(("cancel",))

    mod._finish_child_io(
        writer_thread=writer,
        reader_threads=readers,
        writer_errors=(),
        close_writer=lambda: events.append(("close",)),
        label="test child",
        terminate=lambda: events.append(("terminate",)),
        cancel_io=cancel_io,
        force_cancel=True,
        ignore_writer_errors=True,
    )

    assert events[:3] == [("terminate",), ("cancel",), ("close",)]
    assert writer.joins == 1
    assert all(reader.joins == 1 for reader in readers)


def test_timeout_cleanup_preserves_primary_result_over_broken_stdin() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    events = []

    class StoppedThread:
        def join(self, timeout):
            events.append(("join", timeout))

        def is_alive(self):
            return False

    mod._finish_child_io(
        writer_thread=StoppedThread(),
        reader_threads=(StoppedThread(), StoppedThread()),
        writer_errors=(OSError("broken after timeout"),),
        close_writer=lambda: events.append(("close",)),
        label="test child",
        terminate=lambda: events.append(("terminate",)),
        cancel_io=lambda: events.append(("cancel",)),
        force_cancel=True,
        ignore_writer_errors=True,
    )

    assert events[:3] == [("terminate",), ("cancel",), ("close",)]


def test_cancel_child_pipe_io_targets_every_retained_handle() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    calls = []
    kernel = type(
        "Kernel",
        (),
        {"CancelIoEx": lambda self, handle, overlapped: calls.append((handle, overlapped))},
    )()

    mod._cancel_child_pipe_io(kernel, (11, 22, 33))

    assert calls == [(11, None), (22, None), (33, None)]


def test_runner_uses_offline_token_for_proxy_allowlist(monkeypatch, tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        mod,
        "_open_current_process_token",
        lambda: calls.append("current") or 11,
    )

    def fake_logon(identity):
        calls.append(identity.username)
        return 22

    import opensquilla.sandbox.backend.windows_default_identity as identity_mod

    monkeypatch.setattr(identity_mod, "logon_offline_identity", fake_logon)

    assert mod._open_source_token_for_payload(payload) == 22
    assert calls == ["OpenSquillaSandbox"]


def test_proxy_allowlist_reexecs_helper_under_offline_identity(monkeypatch, tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    calls: list[mod.HelperPayload] = []

    monkeypatch.setattr(
        mod,
        "_resolve_offline_launch_credentials",
        lambda _payload: mod.OfflineLaunchCredentials(
            sid="S-1-test",
            username="OpenSquillaSandbox",
            password="plain",
        ),
    )
    monkeypatch.setattr(mod, "_apply_acl_refresh", lambda _plan, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_run_payload_as_offline_identity",
        lambda request, **_kwargs: calls.append(request) or 7,
        raising=False,
    )
    monkeypatch.setattr(
        mod,
        "_run_restricted_process_native",
        lambda *_args: pytest.fail("offline parent must not launch final process"),
    )

    assert mod._run_windows_default(payload) == 7
    assert calls == [payload]


@pytest.mark.parametrize("failure_stage", ["identity", "decrypt"])
def test_offline_identity_preflight_fails_before_any_acl_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    import opensquilla.sandbox.backend.windows_default_identity as identity_mod
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "none",
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "invalid",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    calls: list[str] = []
    if failure_stage == "identity":
        monkeypatch.setattr(
            identity_mod,
            "offline_identity_from_boundary",
            lambda _boundary: (_ for _ in ()).throw(OSError("identity unavailable")),
        )
    else:
        monkeypatch.setattr(
            identity_mod,
            "offline_identity_from_boundary",
            lambda _boundary: SimpleNamespace(
                sid="S-1-test",
                username="OpenSquillaSandbox",
                protected_password="invalid",
            ),
        )
        monkeypatch.setattr(
            identity_mod,
            "unprotect_password",
            lambda _protected: (_ for _ in ()).throw(OSError("identity unavailable")),
        )
    monkeypatch.setattr(
        mod,
        "_apply_acl_refresh",
        lambda *_args, **_kwargs: calls.append("acl"),
    )
    monkeypatch.setattr(
        mod,
        "_run_payload_as_offline_identity_native",
        lambda *_args, **_kwargs: calls.append("run") or 0,
    )

    with pytest.raises(OSError, match="identity unavailable"):
        mod._run_windows_default(payload)

    assert calls == []


def test_offline_identity_launch_grants_helper_runtime_rx(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    runtime_root = tmp_path / "runtime-python"
    import_root = tmp_path / "src"
    runtime_root.mkdir()
    import_root.mkdir()
    grants: list[tuple[Path, str, str]] = []

    monkeypatch.setattr(
        mod,
        "_offline_helper_runtime_roots",
        lambda: (runtime_root, import_root),
        raising=False,
    )
    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: grants.append((path, access, sid)),
    )
    import opensquilla.sandbox.backend.windows_default_identity as identity_mod

    monkeypatch.setattr(identity_mod, "unprotect_password", lambda _value: "plain")
    monkeypatch.setattr(
        mod,
        "_run_payload_as_offline_identity_native",
        lambda request, *, username, password: 9,
    )

    assert mod._run_payload_as_offline_identity(payload) == 9

    assert grants == [
        (runtime_root, "RX", "S-1-5-21-100-200-300-400"),
        (import_root, "RX", "S-1-5-21-100-200-300-400"),
    ]


def test_offline_identity_launch_grants_payload_acl_roots_to_offline_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsAclPlan": {
                "autoGrants": [
                    {
                        "path": str(tmp_path),
                        "access": "RWX",
                        "kind": "required",
                        "capabilitySid": "S-1-15-3-100-200-300",
                    }
                ],
                "capabilitySids": ["S-1-15-3-100-200-300"],
            },
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    grants: list[tuple[Path, str, str]] = []

    monkeypatch.setattr(mod, "_offline_helper_runtime_roots", lambda: (), raising=False)
    monkeypatch.setattr(
        mod,
        "_grant_path_to_sid",
        lambda path, access, sid: grants.append((path, access, sid)),
    )
    import opensquilla.sandbox.backend.windows_default_identity as identity_mod

    monkeypatch.setattr(identity_mod, "unprotect_password", lambda _value: "plain")
    monkeypatch.setattr(
        mod,
        "_run_payload_as_offline_identity_native",
        lambda request, *, username, password: 9,
    )

    assert mod._run_payload_as_offline_identity(payload) == 9

    assert grants == [(tmp_path, "RWX", "S-1-5-21-100-200-300-400")]


def test_offline_identity_native_launch_sets_all_stdio_handles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import ctypes
    import os
    import sys

    if not sys.platform.startswith("win"):
        pytest.skip("native offline identity launch only runs on Windows")

    import msvcrt

    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd", "/c", "echo ok"),
        cwd=tmp_path,
        env={},
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )
    captured: dict[str, object] = {}
    events: list[str] = []
    next_handle = 1000

    def _handle_value(value: object) -> int:
        return int(getattr(value, "value", value) or 0)

    def _new_handle() -> int:
        nonlocal next_handle
        next_handle += 1
        return next_handle

    class FakeFunction:
        def __init__(self, func):
            self._func = func
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self._func(*args)

    def _create_pipe(read_handle, write_handle, _security_attributes, _size):
        read_handle._obj.value = _new_handle()
        write_handle._obj.value = _new_handle()
        return 1

    def _create_process_with_logon(
        _username,
        _domain,
        _password,
        _logon_flags,
        _application_name,
        _command_line,
        _creation_flags,
        _environment,
        _cwd,
        startup,
        process_info,
    ):
        captured["command_line"] = _command_line.value
        captured["env_block"] = "" if _environment is None else "".join(_environment)
        startup_info = startup._obj
        captured["stdin"] = _handle_value(startup_info.hStdInput)
        captured["stdout"] = _handle_value(startup_info.hStdOutput)
        captured["stderr"] = _handle_value(startup_info.hStdError)
        if not all(captured.values()):
            return 0
        process_info._obj.hProcess = _new_handle()
        process_info._obj.hThread = _new_handle()
        return 1

    def _get_exit_code_process(_process, code):
        code._obj.value = 0
        return 1

    def _write_file(_handle, data, size, written, _overlapped):
        events.append("write_stdin")
        captured["payload_stdin"] = bytes(data[:size])
        written._obj.value = size
        return 1

    class FakeAdvapi32:
        def __init__(self):
            self.CreateProcessWithLogonW = FakeFunction(_create_process_with_logon)

    class FakeKernel32:
        def __init__(self):
            self.CreatePipe = FakeFunction(_create_pipe)
            self.SetHandleInformation = FakeFunction(lambda *_args: 1)
            self.CloseHandle = FakeFunction(lambda *_args: 1)
            self.WaitForSingleObject = FakeFunction(lambda *_args: events.append("wait") or 0)
            self.TerminateProcess = FakeFunction(lambda *_args: 1)
            self.CreateJobObjectW = FakeFunction(lambda *_args: _new_handle())
            self.SetInformationJobObject = FakeFunction(lambda *_args: 1)
            self.AssignProcessToJobObject = FakeFunction(
                lambda *_args: events.append("assign_job") or 1
            )
            self.ResumeThread = FakeFunction(lambda *_args: events.append("resume") or 1)
            self.CancelIoEx = FakeFunction(lambda *_args: 1)
            self.TerminateJobObject = FakeFunction(lambda *_args: 1)
            self.GetExitCodeProcess = FakeFunction(_get_exit_code_process)
            self.WriteFile = FakeFunction(_write_file)
            self.SetErrorMode = FakeFunction(lambda *_args: 0)

    fake_advapi32 = FakeAdvapi32()
    fake_kernel32 = FakeKernel32()

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_kwargs: fake_advapi32 if name == "advapi32" else fake_kernel32,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87)
    monkeypatch.setattr(ctypes, "FormatError", lambda code: "The parameter is incorrect.")
    monkeypatch.setattr(
        msvcrt,
        "open_osfhandle",
        lambda _handle, _flags: os.open(os.devnull, os.O_RDONLY),
    )

    assert (
        mod._run_payload_as_offline_identity_native(
            payload,
            username="OpenSquillaSandbox",
            password="secret",
        )
        == 0
    )
    assert captured["stdin"]
    assert captured["stdout"]
    assert captured["stderr"]
    assert "--payload-stdin" in captured["command_line"]
    assert "--payload-file" not in captured["command_line"]
    assert "--payload-env" not in captured["command_line"]
    assert mod.OFFLINE_PAYLOAD_ENV not in captured["env_block"]
    assert json.loads(captured["payload_stdin"])["offlineChild"] is False
    assert events.index("assign_job") < events.index("resume") < events.index("write_stdin")


def test_offline_reexecuted_helper_uses_current_token(monkeypatch, tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={},
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "base64-dpapi-payload",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
        offline_child=True,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        mod,
        "_open_current_process_token",
        lambda: calls.append("current") or 11,
    )

    assert mod._open_source_token_for_payload(payload) == 11
    assert calls == ["current"]


def test_restricted_process_creation_flags_hide_error_windows() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    flags = mod._restricted_process_creation_flags()

    assert flags & mod.CREATE_NO_WINDOW
    assert flags & mod.CREATE_UNICODE_ENVIRONMENT
    assert flags & mod.CREATE_SUSPENDED
    assert mod._restricted_process_startup_flags() & mod.STARTF_USESHOWWINDOW
    assert mod._runner_error_mode_flags() & mod.SEM_NOOPENFILEERRORBOX
    assert mod._offline_helper_creation_flags() & mod.CREATE_SUSPENDED


def test_restricted_process_application_name_uses_absolute_executable() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    assert mod._restricted_process_application_name((powershell, "-NoProfile")) == powershell
    assert mod._restricted_process_application_name(("powershell.exe", "-NoProfile")) is None


def test_restricted_token_omits_source_user_sid_for_write_capability_token() -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    assert mod._ordered_restricting_sids(
        capability_sids=("cap",),
        user_sid="user",
        logon_sid="logon",
        base_sids=("everyone",),
    ) == ("cap", "logon", "everyone")


def test_runner_overrides_proxy_env_for_proxy_allowlist(tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    payload = mod.HelperPayload(
        argv=("cmd",),
        cwd=tmp_path,
        env={
            "HTTP_PROXY": "http://attacker.invalid:1",
            "HTTPS_PROXY": "http://attacker.invalid:1",
            "NO_PROXY": "localhost",
        },
        policy={
            "network": "proxy_allowlist",
            "network_proxy": {"host": "127.0.0.1", "port": 48123},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-5-21-100-200-300-400",
                "offlineUsername": "OpenSquillaSandbox",
                "protectedPassword": "protected",
                "allowedProxyPorts": [48123],
                "allowLocalBinding": False,
            },
        },
        run_mode="trusted",
        timeout=5,
    )

    env = mod._effective_child_env(payload)

    assert env["HTTP_PROXY"] == "http://127.0.0.1:48123"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:48123"
    assert env["http_proxy"] == "http://127.0.0.1:48123"
    assert env["https_proxy"] == "http://127.0.0.1:48123"
    assert env["ALL_PROXY"] == "http://127.0.0.1:48123"
    assert env["all_proxy"] == "http://127.0.0.1:48123"
    assert env["npm_config_https_proxy"] == "http://127.0.0.1:48123"
    assert env["NPM_CONFIG_HTTPS_PROXY"] == "http://127.0.0.1:48123"
    assert env["PIP_PROXY"] == "http://127.0.0.1:48123"
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert env["NO_PROXY"]
    assert env["no_proxy"] == env["NO_PROXY"]


def test_runner_injects_git_safe_directory_after_existing_git_config(tmp_path) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    (tmp_path / ".git").mkdir()
    payload = mod.HelperPayload(
        argv=("git", "status"),
        cwd=tmp_path,
        env={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.sslBackend",
            "GIT_CONFIG_VALUE_0": "openssl",
        },
        policy={
            "network": "none",
            "windowsAclPlan": {"autoGrants": [], "capabilitySids": []},
            "windowsNetworkBoundary": {
                "offlineUserSid": "S-1-test",
                "offlineUsername": "sandbox",
                "protectedPassword": "protected",
            },
        },
        run_mode="trusted",
        timeout=5,
    )

    env = mod._effective_child_env(payload)

    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "http.sslBackend"
    assert env["GIT_CONFIG_VALUE_0"] == "openssl"
    assert env["GIT_CONFIG_KEY_1"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_1"] == str(tmp_path).replace("\\", "/")
