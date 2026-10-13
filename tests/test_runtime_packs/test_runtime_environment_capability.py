from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla import runtime_packs
from opensquilla.sandbox import runtime_launcher
from opensquilla.sandbox.policy_models import RuntimePolicySettings


def test_management_support_does_not_build_status_or_cache_result(monkeypatch):
    service = SimpleNamespace(management_supported=True)
    monkeypatch.setattr(runtime_packs, "get_runtime_pack_service", lambda _state=None: service)
    assert runtime_packs.management_supported()
    service.management_supported = False
    assert not runtime_packs.management_supported()


@pytest.mark.parametrize("mode", ["safe", "full"])
@pytest.mark.parametrize("require_bundled", [False, True])
def test_runtime_environment_uses_only_enabled_components(
    monkeypatch, tmp_path: Path, mode: str, require_bundled: bool,
):
    calls = []
    host = str(tmp_path / "host")
    managed = str(tmp_path / "python")

    def active_runtime(component):
        calls.append(component)
        return SimpleNamespace(bin_dirs=(Path(managed),))

    def forbidden_status():
        raise AssertionError("environment preparation must not build the status inventory")

    service = SimpleNamespace(
        management_supported=True,
        target="windows-x64" if os.name == "nt" else "linux-x64",
        active_runtime=active_runtime,
        status=forbidden_status,
    )
    monkeypatch.setattr(runtime_packs, "get_runtime_pack_service", lambda _state=None: service)
    policy = RuntimePolicySettings(node=False, git_bash=False)
    result = runtime_launcher.apply_bundled_runtime_path(
        {"Path": host}, mode=mode, policy=policy, require_bundled=require_bundled,
    )
    expected = [managed] if require_bundled else (
        [host, managed] if mode == "full" else [managed, host]
    )
    assert result["Path"].split(os.pathsep) == expected
    assert calls == ["python"]

    calls.clear()
    result = runtime_launcher.apply_bundled_runtime_path(
        {"Path": host}, mode=mode, policy=RuntimePolicySettings(enabled=False),
        require_bundled=require_bundled,
    )
    assert result["Path"] == ("" if require_bundled else host)
    assert calls == []


@pytest.mark.parametrize("require_bundled", [False, True])
def test_management_discovery_error_preserves_runtime_fallback(monkeypatch, require_bundled):
    def unavailable(_state=None):
        raise LookupError("synthetic catalog discovery failure")

    monkeypatch.setattr(runtime_packs, "get_runtime_pack_service", unavailable)
    monkeypatch.setattr(runtime_launcher, "bundled_runtime_resolver", lambda: None)
    assert runtime_packs.management_supported() is False
    result = runtime_launcher.apply_bundled_runtime_path(
        {"PATH": "host"}, mode="full", require_bundled=require_bundled,
    )
    assert result == {"PATH": "" if require_bundled else "host"}
