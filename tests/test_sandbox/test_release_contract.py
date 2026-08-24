from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_public_ui_exposes_only_safe_and_full_modes() -> None:
    source = _text("opensquilla-webui/src/components/chat/ChatComposerRunMode.vue")
    assert "value: 'safe'" in source
    assert "value: 'full'" in source
    for legacy in ("standard", "trusted", "managed", "bypass"):
        assert f"value: '{legacy}'" not in source


def test_saved_safe_policy_is_pinned_at_every_gateway_turn_boundary() -> None:
    for path in (
        "src/opensquilla/gateway/boot.py",
        "src/opensquilla/gateway/channel_dispatch.py",
        "src/opensquilla/gateway/rpc_sessions.py",
        "src/opensquilla/cli/agent_cmd.py",
    ):
        assert "pin_sandbox_policy" in _text(path), path
    assert "sandbox_policy" in _text("src/opensquilla/tools/types.py")


def test_capability_status_requires_live_canaries() -> None:
    capability = _text("src/opensquilla/sandbox/capability_service.py")
    runtime = _text("src/opensquilla/sandbox/setup_runtime.py")
    assert 'SandboxSetupState.READY: "probe_required"' in capability
    assert "_probe_runtime_capabilities" in runtime
    for capability_name in (
        "process",
        "filesystem-worker",
        "denyWriteCarveout",
        "authorityDenyRead",
    ):
        assert capability_name in runtime


def test_recursive_delete_reaches_backup_broker_and_double_confirmation() -> None:
    shell = _text("src/opensquilla/tools/builtin/shell.py")
    assert "_gate_recursive_delete" in shell
    assert "FileMutationBroker" in shell
    assert "BackupTooLarge" in shell
    assert "fs.recursive_delete_without_backup" in shell
    assert "irreversible" in shell


def test_lan_ingress_is_private_and_user_narrowable() -> None:
    auth = _text("src/opensquilla/gateway/auth.py")
    config = _text("src/opensquilla/gateway/config.py")
    assert "Public peers are not accepted" in auth
    assert "allowed_client_cidrs" in auth
    assert "allowed_client_cidrs" in config
    assert "network.subnet_of(parent)" in config


def test_settings_exposes_all_sandbox_sections() -> None:
    panel = _text(
        "opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue"
    )
    for marker in (
        "sandbox-default-mode",
        "builtin-file-rules",
        "recursiveDeleteBackupEnabled",
        "requireApprovalPrefixes",
        "blockAllNetwork",
        "runtimeVersions",
    ):
        assert marker in panel
    assert "create-sandbox-token" not in panel
    assert 'data-testid="sandbox-listen-lan"' not in panel
    assert "allowedClientCidrs" not in panel


def test_formal_runtime_targets_are_pinned_and_windows_bundles_git_bash() -> None:
    manifest = json.loads(
        _text("desktop/electron/runtime/runtime-manifest.json")
    )
    for target in (
        "windows-x64",
        "windows-arm64",
        "linux-x64",
        "linux-arm64",
        "darwin-x64",
        "darwin-arm64",
    ):
        assets = manifest["assets"][target]
        assert assets["python"]["version"]
        assert len(assets["python"]["sha256"]) == 64
        assert assets["node"]["version"]
        assert len(assets["node"]["sha256"]) == 64
        if target.startswith("windows-"):
            assert assets["gitBash"]["executables"]["git"]
            assert assets["gitBash"]["executables"]["bash"]


def test_ci_owns_a_package_contract_verifier() -> None:
    verifier = _text(".github/scripts/verify-sandbox-package.mjs")
    workflow = _text(".github/workflows/ci.yml")
    assert "requiredTargets" in verifier
    assert "package must not contain bundled developer runtimes" in verifier
    assert "runtime/runtime-pack-catalog.json" in verifier
    assert "refusing to publish a desktop package" in verifier
    assert "asset.sizeBytes" in verifier
    assert "asset.unpackedSizeBytes" in verifier
    assert r"\.tar\.xz" in verifier
    assert "verifyInstallerProgressPolicy" in verifier
    assert "verify-sandbox-package.mjs" in workflow

    package_verifier = _text("desktop/electron/scripts/verify-package.mjs")
    assert "verifyInstallerProgressPolicy" in package_verifier

    installer_policy = _text("desktop/electron/scripts/installer-progress-policy.mjs")
    assert "NSIS include must be exactly" in installer_policy
    assert "NSIS must not define a custom full installer script" in installer_policy
    assert "NSIS default build/installer.nsh override must not be present" in installer_policy


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
