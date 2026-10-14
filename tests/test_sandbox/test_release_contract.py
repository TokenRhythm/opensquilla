from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_saved_safe_policy_is_pinned_at_every_gateway_turn_boundary() -> None:
    for path in (
        "src/opensquilla/gateway/boot.py",
        "src/opensquilla/gateway/channel_dispatch.py",
        "src/opensquilla/gateway/direct_turn_runtime.py",
        "src/opensquilla/cli/agent_cmd.py",
    ):
        assert "pin_sandbox_policy" in _text(path), path
    assert "sandbox_policy" in _text("src/opensquilla/tools/types.py")


def test_settings_preserves_command_and_network_policy_wiring() -> None:
    panel = _text(
        "opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue"
    )
    for marker in (
        "requireApprovalPrefixes",
        "blockAllNetwork",
    ):
        assert marker in panel
    assert 'data-testid="sandbox-listen-lan"' not in panel
    assert "allowedClientCidrs" not in panel


def test_windows_runtime_manifest_declares_git_and_bash_entrypoints() -> None:
    manifest = json.loads(
        _text("desktop/electron/runtime/runtime-manifest.json")
    )
    for target in (
        "windows-x64",
        "windows-arm64",
    ):
        assets = manifest["assets"][target]
        assert assets["gitBash"]["executables"]["git"]
        assert assets["gitBash"]["executables"]["bash"]


def test_ci_and_package_verifier_keep_release_policy_wiring() -> None:
    workflow = _text(".github/workflows/ci.yml")
    assert "verify-sandbox-package.mjs" in workflow

    package_verifier = _text("desktop/electron/scripts/verify-package.mjs")
    assert "verifyInstallerProgressPolicy" in package_verifier

def test_finalized_catalog_allows_development_and_release() -> None:
    catalog = json.loads(
        _text("desktop/electron/runtime/runtime-pack-catalog.json")
    )
    assert catalog["finalized"] is True
    assert set(catalog["targets"]) == {
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
        "windows-arm64",
        "windows-x64",
    }

    development = subprocess.run(
        ["node", ".github/scripts/verify-sandbox-package.mjs", "--source"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert development.returncode == 0, development.stderr

    release = subprocess.run(
        ["node", ".github/scripts/verify-sandbox-package.mjs", "--release-source"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert release.returncode == 0, release.stderr
