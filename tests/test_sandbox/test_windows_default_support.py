from __future__ import annotations

from pathlib import Path

import pytest


def test_setup_marker_follows_active_profile_state_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.windows_default_setup import (
        default_setup_marker_path,
    )

    profile_home = tmp_path / "desktop-profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(profile_home))

    assert default_setup_marker_path() == profile_home / "sandbox" / "setup_marker.json"


def test_support_probe_reports_unavailable_off_windows(monkeypatch) -> None:
    from opensquilla.sandbox.backend import windows_default_support as mod

    monkeypatch.setattr(mod.sys, "platform", "linux")

    support = mod.probe_windows_default_support()

    assert support.is_windows is False
    assert support.default_backend_available is False
    assert support.proxy_allowlist_enforced is False


def test_support_probe_requires_setup_marker_on_windows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support as mod

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_ctypes_available", lambda: True)
    monkeypatch.setattr(mod, "_token_api_available", lambda: True)
    monkeypatch.setattr(mod, "_acl_api_available", lambda: True)
    monkeypatch.setattr(
        mod,
        "default_setup_marker_path",
        lambda home=None: tmp_path / "setup_marker.json",
    )

    support = mod.probe_windows_default_support(home=tmp_path)

    assert support.is_windows is True
    assert support.ctypes_available is True
    assert support.setup_ready is False
    assert support.default_backend_available is False
    assert support.requires_admin_setup is True


def test_support_probe_accepts_current_setup_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support as mod
    from opensquilla.sandbox.backend.windows_default_setup import write_setup_marker

    marker = tmp_path / "setup_marker.json"
    write_setup_marker(marker)

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_ctypes_available", lambda: True)
    monkeypatch.setattr(mod, "_token_api_available", lambda: True)
    monkeypatch.setattr(mod, "_acl_api_available", lambda: True)
    monkeypatch.setattr(mod, "default_setup_marker_path", lambda home=None: marker)

    support = mod.probe_windows_default_support(home=tmp_path)

    assert support.setup_ready is True
    assert support.default_backend_available is True
    assert support.proxy_allowlist_enforced is False


def test_persistent_sandbox_dirs_remove_inheritance_and_limit_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from opensquilla.sandbox.backend import windows_default_setup as mod

    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_current_windows_user_sid", lambda: "S-1-real")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command)
            or type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        ),
    )
    marker = tmp_path / ".opensquilla" / "sandbox" / "setup_marker.json"

    mod.lock_persistent_sandbox_dirs(marker, offline_sid="S-1-offline")

    flattened = "\n".join(" ".join(call) for call in calls)
    assert "/inheritance:r" in flattened
    assert "*S-1-real:(OI)(CI)F" in flattened
    assert "*S-1-5-18:(OI)(CI)F" in flattened
    assert "*S-1-5-32-544:(OI)(CI)F" in flattened
    assert "/remove:g *S-1-offline" in flattened


def test_support_probe_requires_network_marker_for_proxy_enforcement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support as mod
    from opensquilla.sandbox.backend.windows_default_setup import write_setup_marker

    marker = tmp_path / "setup_marker.json"
    write_setup_marker(marker)

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_ctypes_available", lambda: True)
    monkeypatch.setattr(mod, "_token_api_available", lambda: True)
    monkeypatch.setattr(mod, "_acl_api_available", lambda: True)
    monkeypatch.setattr(mod, "default_setup_marker_path", lambda home=None: marker)

    support = mod.probe_windows_default_support(home=tmp_path, proxy_ports=(43128,))

    assert support.default_backend_available is True
    assert support.proxy_allowlist_enforced is False


def test_support_probe_accepts_current_network_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support as mod
    from opensquilla.sandbox.backend.windows_default_network import (
        FIREWALL_RULE_VERSION,
        WFP_RULE_VERSION,
        WindowsNetworkSetup,
    )
    from opensquilla.sandbox.backend.windows_default_setup import write_setup_marker

    marker = tmp_path / "setup_marker.json"
    write_setup_marker(
        marker,
        network=WindowsNetworkSetup(
            offline_user_sid="S-1-5-21-100-200-300-400",
            allowed_proxy_ports=(43128,),
            allow_local_binding=False,
            firewall_rule_version=FIREWALL_RULE_VERSION,
            wfp_rule_version=WFP_RULE_VERSION,
        ),
    )

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_ctypes_available", lambda: True)
    monkeypatch.setattr(mod, "_token_api_available", lambda: True)
    monkeypatch.setattr(mod, "_acl_api_available", lambda: True)
    monkeypatch.setattr(mod, "default_setup_marker_path", lambda home=None: marker)

    support = mod.probe_windows_default_support(home=tmp_path)

    assert support.proxy_allowlist_enforced is True


def test_support_probe_rejects_legacy_firewall_network_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support as mod
    from opensquilla.sandbox.backend.windows_default_network import (
        WFP_RULE_VERSION,
        WindowsNetworkSetup,
    )
    from opensquilla.sandbox.backend.windows_default_setup import write_setup_marker

    marker = tmp_path / "setup_marker.json"
    write_setup_marker(
        marker,
        network=WindowsNetworkSetup(
            offline_user_sid="S-1-5-21-100-200-300-400",
            allowed_proxy_ports=(43128,),
            allow_local_binding=False,
            firewall_rule_version=1,
            wfp_rule_version=WFP_RULE_VERSION,
        ),
    )

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_ctypes_available", lambda: True)
    monkeypatch.setattr(mod, "_token_api_available", lambda: True)
    monkeypatch.setattr(mod, "_acl_api_available", lambda: True)
    monkeypatch.setattr(mod, "default_setup_marker_path", lambda home=None: marker)

    support = mod.probe_windows_default_support(home=tmp_path)

    assert support.default_backend_available is True
    assert support.proxy_allowlist_enforced is False
