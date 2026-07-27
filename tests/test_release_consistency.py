from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

CURRENT_VERSION = "0.5.0"
CURRENT_DESKTOP_VERSION = "0.5.0"
CURRENT_TAG = f"v{CURRENT_VERSION}"
HISTORICAL_PREVIEW_VERSION = "0.2.0rc1"
HISTORICAL_PREVIEW_TAG = f"v{HISTORICAL_PREVIEW_VERSION}"


def test_pyproject_version_matches_current_release() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = config["project"]["version"]
    assert version == CURRENT_VERSION, (
        f"pyproject.toml version must match the current release; got '{version}'"
    )


def test_lockfile_version_matches_current_release() -> None:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in lock["package"] if item["name"] == "opensquilla")

    assert package["version"] == CURRENT_VERSION


def test_desktop_electron_release_config_matches_current_release() -> None:
    package = json.loads(Path("desktop/electron/package.json").read_text(encoding="utf-8"))
    lock = json.loads(Path("desktop/electron/package-lock.json").read_text(encoding="utf-8"))
    build = package["build"]

    assert package["version"] == CURRENT_DESKTOP_VERSION
    assert lock["version"] == CURRENT_DESKTOP_VERSION
    assert lock["packages"][""]["version"] == CURRENT_DESKTOP_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", package["version"])
    assert not re.search(r"(?<=\d)(?:a|b|rc)\d+$", package["version"])
    assert package["repository"] == {
        "type": "git",
        "url": "https://github.com/opensquilla/opensquilla.git",
    }
    assert build["artifactName"] == "OpenSquilla-${version}-${os}-${arch}.${ext}"
    assert build["mac"]["target"] == ["dmg", "zip"]
    assert build["mac"].get("identity", "auto") is not None
    assert build["win"]["target"] == ["nsis"]
    assert build["nsis"]["oneClick"] is False
    assert build["nsis"]["allowToChangeInstallationDirectory"] is True
    assert build["nsis"]["deleteAppDataOnUninstall"] is False
    package_verifier = Path("desktop/electron/scripts/verify-package.mjs").read_text(
        encoding="utf-8"
    )
    assert "deleteAppDataOnUninstall !== false" in package_verifier


def test_release_workflow_builds_desktop_installers() -> None:
    workflow = Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")

    assert "name: Release Assets" in workflow
    assert "build-desktop-macos:" in workflow
    assert "build-desktop-windows:" in workflow
    assert "npx electron-builder --mac --publish never" in workflow
    assert "npx electron-builder --win --publish never" in workflow
    assert "desktop_asset_version" in workflow
    assert "OpenSquilla-{desktop_version}-mac-arm64.dmg" in workflow
    assert "OpenSquilla-{desktop_version}-win-x64.exe" in workflow
    assert "latest-mac.yml" in workflow
    assert "latest.yml" in workflow
    assert 'NOTES_FILE="docs/releases/${TAG#v}.md"' in workflow
    assert '--notes-file "${NOTES_FILE}"' in workflow
    assert 'gh release upload "${TAG}" dist/* --clobber' in workflow


def test_tui_companion_remains_development_only() -> None:
    """A normal version tag must not publish the in-repo development host."""

    workflow_text = Path(".github/workflows/wheelhouse-release.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]

    assert "build-tui-host-macos" not in jobs
    assert "build-tui-host-linux" not in jobs
    assert "opensquilla_tui_host-" not in workflow_text
    assert "write_tui_release_manifest.py" not in workflow_text
    assert "dist/install.sh" not in workflow_text

    installer = Path("install.sh").read_text(encoding="utf-8")
    assert "--tui-host-only" not in installer
    assert "opensquilla_tui_host-" not in installer


def _release_upload_script() -> str:
    workflow = yaml.safe_load(
        Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")
    )
    return next(
        step["run"]
        for step in workflow["jobs"]["publish-release"]["steps"]
        if step.get("name") == "Upload to GitHub Release"
    )


def _run_release_upload_with_fake_gh(
    tmp_path: Path,
    *,
    tag: str,
    draft: bool,
    prerelease: bool,
) -> tuple[subprocess.CompletedProcess[str], str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    call_log = tmp_path / "gh-calls.log"
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_GH_LOG"
if [[ "$*" == *"--json"* ]]; then
  printf '%s\\n' "$FAKE_RELEASE_STATE"
fi
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "FAKE_GH_LOG": str(call_log),
            "FAKE_RELEASE_STATE": json.dumps(
                {"assets": [], "isDraft": draft, "isPrerelease": prerelease}
            ),
            "GH_REPO": "opensquilla/opensquilla",
            "GH_TOKEN": "synthetic-test-token",
            "PATH": (
                f"{fake_bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
            ),
            "TAG": tag,
        }
    )
    result = subprocess.run(
        ["bash", "-c", _release_upload_script()],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    return result, calls


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release upload Bash step runs in the required Ubuntu packaging job.",
)
def test_release_upload_refuses_to_mutate_an_existing_public_release(tmp_path: Path) -> None:
    result, calls = _run_release_upload_with_fake_gh(
        tmp_path,
        tag="v9.9.9rc1",
        draft=False,
        prerelease=True,
    )

    assert result.returncode != 0
    assert "non-Draft" in result.stderr
    for mutating_call in (
        "release create",
        "release edit",
        "release delete-asset",
        "release upload",
    ):
        assert mutating_call not in calls


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release upload Bash step runs in the required Ubuntu packaging job.",
)
def test_release_upload_derives_preview_state_from_each_tag(tmp_path: Path) -> None:
    stable_result, stable_calls = _run_release_upload_with_fake_gh(
        tmp_path / "stable",
        tag="v9.9.9",
        draft=True,
        prerelease=False,
    )
    preview_result, preview_calls = _run_release_upload_with_fake_gh(
        tmp_path / "preview",
        tag="v9.9.9rc7",
        draft=True,
        prerelease=True,
    )

    assert stable_result.returncode == 0, stable_result.stderr
    assert preview_result.returncode == 0, preview_result.stderr
    assert "release upload v9.9.9" in stable_calls
    assert "release upload v9.9.9rc7" in preview_calls


def test_release_profile_preservation_probe_covers_identity_config_and_chat_db(
    tmp_path: Path,
) -> None:
    probe = Path(".github/scripts/verify-release-profile-preservation.py")
    home = tmp_path / "Application Support" / "OpenSquilla" / "opensquilla"
    label = "contract-probe"

    subprocess.run(
        [sys.executable, str(probe), "seed", "--home", str(home), "--label", label],
        check=True,
        text=True,
        capture_output=True,
    )
    verified = subprocess.run(
        [sys.executable, str(probe), "verify", "--home", str(home), "--label", label],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "profile preservation verified" in verified.stdout

    reseed = subprocess.run(
        [sys.executable, str(probe), "seed", "--home", str(home), "--label", label],
        check=False,
        text=True,
        capture_output=True,
    )
    assert reseed.returncode != 0
    assert "refusing to overwrite" in reseed.stderr

    identity = home / "workspace" / "IDENTITY.md"
    identity.write_text("changed\n", encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(probe), "verify", "--home", str(home), "--label", label],
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0
    assert "IDENTITY.md" in rejected.stderr

    identity.write_text(f"# Synthetic {label} identity sentinel\n", encoding="utf-8")
    with sqlite3.connect(home / "state" / "sessions.db") as connection:
        connection.execute("UPDATE release_preservation_chat SET body = 'changed'")
    rejected = subprocess.run(
        [sys.executable, str(probe), "verify", "--home", str(home), "--label", label],
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0
    assert "sessions.db retained-chat row changed" in rejected.stderr


def test_release_workflow_gates_built_and_downloaded_installers_on_profile_retention() -> None:
    workflow = Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")
    mac_helper = Path(".github/scripts/verify-release-macos-upgrade.sh").read_text(encoding="utf-8")
    windows_helper = Path(".github/scripts/verify-release-windows-upgrade.ps1").read_text(
        encoding="utf-8"
    )
    update_banner_smoke = Path(
        "desktop/electron/scripts/test-packaged-update-banner.mjs"
    ).read_text(encoding="utf-8")
    probe = Path(".github/scripts/verify-release-profile-preservation.py").read_text(
        encoding="utf-8"
    )

    mac_build = workflow[
        workflow.index("  build-desktop-macos:") : workflow.index("  build-desktop-windows:")
    ]
    windows_build = workflow[
        workflow.index("  build-desktop-windows:") : workflow.index("  publish-release:")
    ]
    assert "verify-release-macos-upgrade.sh" in mac_build
    assert "verify-release-windows-upgrade.ps1" in windows_build
    assert "-VerifyLongRunningUpdateBanner" in windows_build
    assert mac_build.index("verify-release-macos-upgrade.sh") < mac_build.index(
        "Upload macOS Electron artifacts"
    )
    assert windows_build.index("verify-release-windows-upgrade.ps1") < windows_build.index(
        "Upload Windows Electron artifacts"
    )

    for artifact in (
        "config.toml",
        "IDENTITY.md",
        "USER.md",
        "SOUL.md",
        "MEMORY.md",
        "sessions.db",
        "PRAGMA quick_check",
        "synthetic retained chat",
    ):
        assert artifact in probe
    for helper in (mac_helper, windows_helper):
        assert "v0.5.0rc3" in helper
        assert "recovery inspect" in helper
        assert "verify-release-profile-preservation.py" in helper
        assert "workspace" in helper
        assert "state" in helper

    assert "test-packaged-update-banner.mjs" in windows_helper
    assert "if ($VerifyLongRunningUpdateBanner)" in windows_helper
    assert windows_helper.index("$launched = Start-Process") < windows_helper.index(
        "if ($VerifyLongRunningUpdateBanner)"
    )
    assert "OPENSQUILLA_UPDATE_CHECK_ENDPOINT" in update_banner_smoke
    assert "schemaVersion: 1" in update_banner_smoke
    assert "baseVersion" in update_banner_smoke
    assert "tag_name" not in update_banner_smoke
    assert "visibilitychange" in update_banner_smoke
    assert "requestCount, 1" in update_banner_smoke
    assert "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY" in update_banner_smoke
    assert "writeSyntheticUpdateCache(privacyUserDataDir, baseVersion, 0)" in update_banner_smoke
    assert "writeSyntheticCanonicalWorkspace(privacyUserDataDir)" in update_banner_smoke
    assert update_banner_smoke.index(
        "writeSyntheticCanonicalWorkspace(privacyUserDataDir)"
    ) < update_banner_smoke.index("privacyApp = await launchCandidate(")
    assert "GITHUB_ACTIONS: '0'" in update_banner_smoke

    mac_audit = workflow[
        workflow.index("  audit-downloaded-macos-release:") : workflow.index(
            "  audit-downloaded-windows-release:"
        )
    ]
    windows_audit = workflow[workflow.index("  audit-downloaded-windows-release:") :]
    for audit in (mac_audit, windows_audit):
        assert "needs: publish-release" in audit
        assert "contents: write" in audit
        assert "contents: read" not in audit
        assert "gh release download" in audit
        assert "SHA256SUMS" in audit
        assert "isDraft" in audit
    assert "codesign --verify --deep --strict" in mac_audit
    assert "spctl -a -vv -t exec" in mac_audit
    assert "xcrun stapler validate" in mac_audit
    assert "@electron/asar@3.4.1 extract-file" in mac_audit
    assert "verify-release-macos-upgrade.sh" in mac_audit
    assert "Get-FileHash -Algorithm SHA256" in windows_audit
    assert "verify-release-windows-upgrade.ps1" in windows_audit


def test_manual_release_workflow_without_a_tag_only_uploads_aggregate_artifacts() -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")
    )
    publish_steps = workflow["jobs"]["publish-release"]["steps"]
    aggregate = next(
        step for step in publish_steps if step["name"] == "Upload aggregate workflow artifact"
    )
    github_upload = next(
        step for step in publish_steps if step["name"] == "Upload to GitHub Release"
    )

    assert "if" not in aggregate
    assert "github.event.inputs.tag != ''" in github_upload["if"]
    for job_name in ("audit-downloaded-macos-release", "audit-downloaded-windows-release"):
        assert "github.event.inputs.tag != ''" in workflow["jobs"][job_name]["if"]


def test_release_workflow_hydrates_and_smokes_desktop_router_runtime() -> None:
    workflow = Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")

    for job_name in ["build-desktop-macos", "build-desktop-windows"]:
        start = workflow.index(f"  {job_name}:")
        end = len(workflow)
        for next_job in ["build-desktop-windows", "publish-release"]:
            marker = f"\n  {next_job}:"
            pos = workflow.find(marker, start + 1)
            if pos != -1:
                end = min(end, pos)
        job = workflow[start:end]
        assert "lfs: true" in job
        assert 'git lfs pull --include="src/opensquilla/squilla_router/models/**"' in job
        assert "npm run build:gateway" in job
        assert "npm run verify:package" in job
        assert "npm run verify:gateway-smoke" in job
        assert 'OPENSQUILLA_REQUIRE_PACKAGED_GATEWAY_SMOKE: "1"' in job
        if job_name == "build-desktop-macos":
            assert 'OPENSQUILLA_GATEWAY_SMOKE_TIMEOUT_MS: "240000"' in job
        else:
            assert "OPENSQUILLA_GATEWAY_SMOKE_TIMEOUT_MS" not in job


def test_release_workflow_keeps_macos_signing_identity_auto_selected() -> None:
    workflow = Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")
    mac_step = workflow.split("- name: Build signed macOS installer", 1)[1].split(
        "- name: Verify Electron package", 1
    )[0]

    assert "CSC_LINK: ${{ secrets.MAC_CSC_LINK }}" in mac_step
    assert "CSC_KEY_PASSWORD: ${{ secrets.MAC_CSC_KEY_PASSWORD }}" in mac_step
    assert "APPLE_ID: ${{ secrets.APPLE_ID }}" in mac_step
    assert "CSC_NAME" not in mac_step
    assert "GH_TOKEN" not in mac_step


def test_release_workflow_keeps_windows_build_unsigned_until_signing_is_available() -> None:
    workflow = Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")
    windows_step = workflow.split("- name: Build unsigned Windows installer", 1)[1].split(
        "- name: Verify Electron package", 1
    )[0]

    assert "npx electron-builder --win --publish never" in windows_step
    assert 'CSC_IDENTITY_AUTO_DISCOVERY: "false"' in windows_step
    assert not Path("desktop/electron/electron-builder.release.cjs").exists()

    for env_name in [
        "OPENSQUILLA_WINDOWS_AZURE_SIGNING",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TRUSTED_SIGNING_PUBLISHER_NAME",
        "AZURE_TRUSTED_SIGNING_ENDPOINT",
        "AZURE_TRUSTED_SIGNING_ACCOUNT_NAME",
        "AZURE_TRUSTED_SIGNING_CERTIFICATE_PROFILE_NAME",
    ]:
        assert env_name not in windows_step

    assert "azureSignOptions" not in workflow
    assert "forceCodeSigning: true" not in workflow
    assert "timestampRfc3161: 'http://timestamp.acs.microsoft.com'" not in workflow


def test_release_docs_describe_unsigned_windows_policy() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    localized_readmes = {
        "zh-Hans": Path("README.zh-Hans.md").read_text(encoding="utf-8"),
        "ja": Path("README.ja.md").read_text(encoding="utf-8"),
        "fr": Path("README.fr.md").read_text(encoding="utf-8"),
        "de": Path("README.de.md").read_text(encoding="utf-8"),
        "es": Path("README.es.md").read_text(encoding="utf-8"),
    }
    releases = Path("RELEASES.md").read_text(encoding="utf-8")
    release_notes = Path(f"docs/releases/{CURRENT_VERSION}.md").read_text(encoding="utf-8")
    signing_policy = Path("docs/code-signing-policy.md").read_text(encoding="utf-8")
    privacy_policy = Path("PRIVACY.md").read_text(encoding="utf-8")

    assert "Code signing policy:" in readme
    assert "Windows builds are currently unsigned" in readme
    assert "Windows desktop installer is currently unsigned" in releases
    assert "Windows release builds are currently unsigned" in signing_policy
    assert "claim Windows code signing" in signing_policy
    assert "[`PRIVACY.md`](../PRIVACY.md)" in signing_policy
    assert "[@Open-Squilla](https://github.com/Open-Squilla)" in signing_policy
    assert "Initial SignPath approvers" in signing_policy
    assert "network observability" in signing_policy

    for text in [readme, releases, release_notes]:
        assert "code-signing-policy.md" in text

    assert "PRIVACY.md" in readme
    assert "THIRD_PARTY_NOTICES.md" in readme
    assert "Installation Telemetry" in privacy_policy
    assert "OPENSQUILLA_TELEMETRY_DISABLED=true" in privacy_policy
    assert "future signing plan" not in readme

    for text in [readme, releases]:
        assert "signed desktop installers" not in text

    for locale, phrase in {
        "zh-Hans": "已签名的桌面",
        "ja": "署名済みのデスクトップ",
        "fr": "installateurs de bureau signés",
        "de": "signierten Desktop",
        "es": "instaladores de escritorio firmados",
    }.items():
        assert phrase not in localized_readmes[locale]


def test_release_docs_warn_rc3_users_to_upgrade_in_place() -> None:
    readmes = [
        Path("README.md"),
        Path("README.zh-Hans.md"),
        Path("README.ja.md"),
        Path("README.fr.md"),
        Path("README.de.md"),
        Path("README.es.md"),
    ]
    for path in readmes:
        text = path.read_text(encoding="utf-8")
        assert "RC3" in text, path
        assert "RC4" in text, path
        assert r"%APPDATA%\OpenSquilla" in text, path

    releases = Path("RELEASES.md").read_text(encoding="utf-8")
    current_notes = Path(f"docs/releases/{CURRENT_VERSION}.md").read_text(encoding="utf-8")
    assert "must install the\nnew version directly over the existing installation" in releases
    assert "must not uninstall RC3\nfirst" in releases
    assert "deleteAppDataOnUninstall=false" in releases
    assert "uninstall a pre-Preview-4 build first" in current_notes


def test_privacy_docs_describe_network_observability_controls() -> None:
    docs = {
        "README.md": Path("README.md").read_text(encoding="utf-8"),
        "README.zh-Hans.md": Path("README.zh-Hans.md").read_text(encoding="utf-8"),
        "PRIVACY.md": Path("PRIVACY.md").read_text(encoding="utf-8"),
        "RELEASES.md": Path("RELEASES.md").read_text(encoding="utf-8"),
        f"docs/releases/{CURRENT_VERSION}.md": Path(
            f"docs/releases/{CURRENT_VERSION}.md"
        ).read_text(encoding="utf-8"),
        "docs/code-signing-policy.md": Path("docs/code-signing-policy.md").read_text(
            encoding="utf-8"
        ),
    }

    for path, text in docs.items():
        assert "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true" in text, path
        assert "disable_network_observability = true" in text, path
        assert "OPENSQUILLA_TELEMETRY_DISABLED=true" in text, path
        assert "OPENSQUILLA_UPDATE_CHECK_DISABLED=true" in text, path

    privacy = docs["PRIVACY.md"]
    assert "automatic install telemetry" in privacy
    assert "passive update checks" in privacy
    assert "automatic desktop update checks at startup" in privacy
    assert "during long-running app sessions" in privacy
    assert "Manual user-initiated actions may still contact network services" in privacy
    assert "do not bypass the\nunified or legacy opt-out controls" in privacy
    assert "Explicit update-availability checks remain disabled" in docs["README.md"]
    assert "用户显式触发的更新可用性检查也不会绕过它" in docs["README.zh-Hans.md"]
    assert "update-availability checks do not bypass these controls" in docs["RELEASES.md"]


def test_release_docs_describe_channel_aware_long_running_update_checks() -> None:
    releases = Path("RELEASES.md").read_text(encoding="utf-8")

    assert "Stable builds only\noffer newer stable releases" in releases
    assert "a later RC or the final stable release" in releases
    assert "never\njump to a preview on a different base" in releases
    assert "included starting with RC4" in releases
    assert "Windows RC3 still requires a manual, in-place RC4 upgrade" in releases


def _dep_names(specs: list[str]) -> set[str]:
    names: set[str] = set()
    for spec in specs:
        head = spec.strip()
        for sep in ("[", " ", ";", "=", ">", "<", "~", "!"):
            head = head.split(sep, 1)[0]
        if head:
            names.add(head.lower())
    return names


def test_recommended_extra_uses_onnx_tokenizers_without_transformers() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    recommended = config["project"]["optional-dependencies"]["recommended"]

    assert any(dep.startswith("onnxruntime") for dep in recommended)
    assert any(dep.startswith("tokenizers") for dep in recommended)
    assert not any(dep.startswith("transformers") for dep in recommended)


def test_default_recommended_install_contract_covers_router_and_channels() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    dependencies = _dep_names(project["dependencies"])
    extras = project["optional-dependencies"]
    recommended = _dep_names(extras["recommended"])

    assert {
        "lightgbm",
        "numpy",
        "onnxruntime",
        "scikit-learn",
        "tokenizers",
    } <= recommended
    assert {
        "cryptography",  # WeCom callback crypto
        "dingtalk-stream",
        "httpx",  # Slack, Telegram, Feishu, WeCom HTTP calls
        "lark-oapi",
        "python-telegram-bot",
        "qq-botpy",
        "websockets",  # Discord gateway and Feishu SDK transport
    } <= dependencies
    for alias in ("feishu", "telegram", "dingtalk", "wecom", "qq"):
        assert alias not in extras

    assert "matrix-nio" in "\n".join(extras["matrix"])


def test_core_dependencies_support_default_pptx_skill() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = config["project"]["dependencies"]

    assert any(dep.startswith("python-pptx") for dep in dependencies)


def test_releases_md_exists_and_references_current_and_preview_tags() -> None:
    releases = Path("RELEASES.md")
    assert releases.is_file(), "RELEASES.md must exist at the repository root"
    text = releases.read_text(encoding="utf-8")
    assert CURRENT_TAG in text, f"RELEASES.md must reference the tag '{CURRENT_TAG}'"
    assert HISTORICAL_PREVIEW_TAG in text, (
        f"RELEASES.md must retain the historical tag '{HISTORICAL_PREVIEW_TAG}'"
    )
    assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-mac-arm64.dmg" in text
    assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-win-x64.exe" in text
    assert "do not publish Windows portable zips" in text
    assert "legacy Windows portable downloads" in text
    assert "separately branded macOS or Linux portable bundles" in text
    assert "macOS `.zip` is the Electron desktop and updater artifact" in text
    assert "macOS portable zips" not in text
    assert "`0.5.0rc5` /\n    `v0.5.0rc5`" in text
    assert "tracks the most recently pushed release tag" in text


def test_changelog_has_current_release_section_and_unreleased() -> None:
    changelog = Path("CHANGELOG.md")
    assert changelog.is_file(), "CHANGELOG.md must exist at the repository root"
    text = changelog.read_text(encoding="utf-8")
    assert f"[{CURRENT_VERSION}]" in text, (
        f"CHANGELOG.md must contain a [{CURRENT_VERSION}] section"
    )
    assert "[Unreleased]" in text, "CHANGELOG.md must retain an [Unreleased] section"


def test_readme_release_install_uses_latest_assets_and_pinned_alternative() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-mac-arm64.dmg" in readme
    assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-win-x64.exe" in readme
    assert "versioned GitHub assets" in readme
    assert "Alibaba Cloud OSS mirror" in readme
    assert "Portable archives remain retired" in readme
    assert "releases/latest/download/OpenSquilla-windows-x64-portable.zip" not in readme
    assert (
        f"releases/download/{CURRENT_TAG}/opensquilla-{CURRENT_VERSION}-py3-none-any.whl" in readme
    )
    assert "opensquilla-latest-py3-none-any.whl" not in readme
    assert "Python wheel installs use versioned wheel filenames" in readme
    assert "Release install commands use published GitHub release assets" in readme


def test_all_readmes_default_install_paths_to_the_current_preview() -> None:
    wheel_url = f"releases/download/{CURRENT_TAG}/opensquilla-{CURRENT_VERSION}-py3-none-any.whl"
    readmes = [
        Path("README.md"),
        Path("README.zh-Hans.md"),
        Path("README.ja.md"),
        Path("README.fr.md"),
        Path("README.de.md"),
        Path("README.es.md"),
    ]

    oss_latest_assets = (
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/"
        "OpenSquilla-mac-arm64.dmg",
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/"
        "OpenSquilla-win-x64.exe",
    )

    for path in readmes:
        text = path.read_text(encoding="utf-8")
        assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-mac-arm64.dmg" in text, path
        assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-win-x64.exe" in text, path
        assert all(url in text for url in oss_latest_assets), path
        assert wheel_url in text, path
        assert "ghcr.io/opensquilla/opensquilla:latest" in text, path
        assert "0.5.0-Preview-2-Desktop" not in text, path


def test_user_facing_install_docs_use_current_release_wheel() -> None:
    current_wheel_url = (
        f"releases/download/{CURRENT_TAG}/opensquilla-{CURRENT_VERSION}-py3-none-any.whl"
    )
    wheel_url_pattern = re.compile(
        r"releases/download/v(?P<tag_version>[^/]+)/"
        r"opensquilla-(?P<file_version>[^/]+)-py3-none-any\.whl"
    )
    install_docs = [
        Path("README.md"),
        Path("README.product.md"),
        Path("docs/quickstart.md"),
        Path("docs/cli.md"),
        Path("docs/mcp-server.md"),
        Path("docs/operations.md"),
    ]

    for path in install_docs:
        text = path.read_text(encoding="utf-8")
        wheel_urls = list(wheel_url_pattern.finditer(text))

        assert wheel_urls, f"{path} must include a pinned release wheel URL"
        assert current_wheel_url in text, f"{path} must install from {CURRENT_TAG}"
        for match in wheel_urls:
            assert match.group("tag_version") == CURRENT_VERSION
            assert match.group("file_version") == CURRENT_VERSION


def test_release_installers_default_to_current_tag() -> None:
    for path in [Path("install.sh"), Path("install.ps1")]:
        text = path.read_text(encoding="utf-8")
        assert CURRENT_TAG in text
        assert "opensquilla-$releaseVersion-py3-none-any.whl" in text or (
            "opensquilla-${release_version}-py3-none-any.whl" in text
        )
        assert "opensquilla-latest-py3-none-any.whl" not in text


def test_release_workflow_marks_preview_tags_as_prereleases() -> None:
    workflow = Path(".github/workflows/wheelhouse-release.yml").read_text(encoding="utf-8")

    assert "IS_PRERELEASE" in workflow
    assert "--prerelease" in workflow
    assert "OpenSquilla {match.group(1)} Preview {match.group(2)}" in workflow
    assert "0.5+ release assets must not include Windows portable zips" in workflow
    assert "OpenSquilla-windows-x64-portable.zip" not in workflow
    assert "opensquilla-latest-py3-none-any.whl" not in workflow


def test_container_workflow_gates_latest_promotion() -> None:
    workflow = Path(".github/workflows/docker-image.yml").read_text(encoding="utf-8")

    assert 'tags:\n      - "v*"' in workflow
    assert 'tag_version="${GITHUB_REF_NAME#v}"' in workflow
    assert 'project["project"]["version"]' in workflow
    assert "does not match project version" in workflow
    assert "packages: write" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "type=ref,event=tag" in workflow
    assert "type=raw,value=latest" not in workflow
    assert "provenance: false" in workflow
    assert "OPENSQUILLA_FORBID_PERSONAL_BGM=1" in workflow
    assert "most recently pushed release tag" in workflow
    assert '["docker", "buildx", "imagetools", "inspect", image_ref, "--raw"]' in workflow
    assert 'expected = {"linux/amd64", "linux/arm64"}' in workflow
    assert 'docker run --detach --pull=always "${IMAGE_REF}"' in workflow
    assert ".State.Health.Status" in workflow
    assert '[[ "${health}" == "healthy" ]]' in workflow
    assert "docker buildx imagetools create" in workflow

    build = workflow.index("- name: Build multi-arch image")
    verify = workflow.index("- name: Verify pushed manifest platforms")
    smoke = workflow.index("- name: Smoke pushed image HEALTHCHECK")
    promote = workflow.index("- name: Promote verified release image to latest")
    assert build < verify < smoke < promote


def test_historical_040_release_notes_remain_available() -> None:
    notes = Path("docs/releases/0.4.0.md").read_text(encoding="utf-8")

    assert "# OpenSquilla 0.4.0" in notes
    assert "OpenSquilla-0.4.0-mac-arm64.dmg" in notes


def test_current_release_notes_cover_recovery_transfer_upgrade_and_containers() -> None:
    notes = Path(f"docs/releases/{CURRENT_VERSION}.md").read_text(encoding="utf-8")

    assert "## Downloads" in notes
    assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-mac-arm64.dmg" in notes
    assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-mac-arm64.zip" in notes
    assert f"OpenSquilla-{CURRENT_DESKTOP_VERSION}-win-x64.exe" in notes
    assert f"opensquilla-{CURRENT_VERSION}-py3-none-any.whl" in notes
    assert notes.index("### Model Ensemble and multi-provider routing") < notes.index(
        "### Safer upgrades, migration, and profile-data protection"
    )
    assert notes.index("### Channels and runtime reliability") < notes.index(
        "### Container images and mainland-China downloads"
    )
    assert notes.index("## Since Preview 4") < notes.index("## Downloads")
    assert "Normal version upgrades do not require a data transfer" in notes
    assert "not silently overwritten, deleted, or merged" in notes
    assert "never silently merged" in notes
    assert "automatic sync" in notes
    assert "No Windows Portable assets are published for 0.5.0" in notes
    assert "0.5.0 Portable zip" in notes
    assert "## Upgrading from 0.4.1 or a 0.5.0 preview" in notes
    assert "uninstall a pre-Preview-4 build first" in notes
    assert r"%APPDATA%\OpenSquilla" in notes
    assert "ghcr.io/opensquilla/opensquilla:v0.5.0" in notes
    assert "`latest` tag follows the most recently verified release tag" in notes
    assert (
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/"
        "OpenSquilla-mac-arm64.dmg" in notes
    )
    assert (
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/"
        "OpenSquilla-win-x64.exe" in notes
    )
    assert "releases/latest.html" not in notes
    assert "Synthetic fixtures" not in notes
    assert "release gate" not in notes
    assert "## Acknowledgements" in notes
    for login in [
        "@HuaXiawithMoon",
        "@ab2ence",
        "@nice-code-la",
        "@nankingjing",
    ]:
        assert login in notes
    assert "CONTRIBUTORS.md" in notes


def test_docs_index_links_current_release_notes() -> None:
    index = Path("docs/README.md").read_text(encoding="utf-8")

    assert f"releases/{CURRENT_VERSION}.md" in index
    assert "releases/0.4.0.md" in index


def test_current_contributor_ledger_records_050rc4_attribution() -> None:
    ledger = Path("CONTRIBUTORS.md").read_text(encoding="utf-8")
    section = ledger.split("## OpenSquilla 0.5.0", 1)[1].split("## OpenSquilla 0.5.0rc3", 1)[0]

    expected = {
        "@HuaXiawithMoon": "#582",
        "@ab2ence": "#586",
        "@nice-code-la": "#588",
        "@nankingjing": "#598",
    }
    for login, evidence in expected.items():
        assert login in section
        assert evidence in section
    assert "#636" in section
    assert "Codex" not in section
    assert "Claude Code" not in section
