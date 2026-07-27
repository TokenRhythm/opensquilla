import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_PS1 = ROOT / "install.ps1"
RELEASE_SH = ROOT / "install.sh"
SOURCE_PS1 = ROOT / "scripts" / "install_source.ps1"
SOURCE_SH = ROOT / "scripts" / "install_source.sh"
CURRENT_RELEASE_TAG = "v0.5.0"


def test_source_install_scripts_force_refresh_local_uv_tool_package() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    sh = SOURCE_SH.read_text(encoding="utf-8")

    assert "'--force', '--reinstall-package', 'opensquilla'" in ps1
    assert "--force --reinstall-package opensquilla" in sh


def test_install_scripts_do_not_run_onboarding_or_gateway() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        RELEASE_SH.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
        SOURCE_SH.read_text(encoding="utf-8"),
    ]

    for script in scripts:
        assert "onboard --if-needed" not in script
        assert "& opensquilla onboard" not in script
        assert "& opensquilla gateway run" not in script
        assert '"opensquilla onboard"' not in script
        assert '"opensquilla gateway run"' not in script


def test_release_installers_install_version_pinned_wheel_with_uv() -> None:
    ps1 = RELEASE_PS1.read_text(encoding="utf-8")
    sh = RELEASE_SH.read_text(encoding="utf-8")

    for script in (ps1, sh):
        assert CURRENT_RELEASE_TAG in script
        assert "opensquilla-$releaseVersion-py3-none-any.whl" in script or (
            "opensquilla-${release_version}-py3-none-any.whl" in script
        )
        assert "opensquilla-latest-py3-none-any.whl" not in script
        assert "releases/latest/download" not in script
        assert "--python" in script
        assert "--force" in script
        assert "--reinstall-package" in script
        assert "recommended" in script
        assert "https://astral.sh/uv/install" in script
        assert "Next steps:" in script


def test_release_installer_rejects_non_release_selectors() -> None:
    ps1 = RELEASE_PS1.read_text(encoding="utf-8")

    if not sys.platform.startswith("win"):
        result = subprocess.run(
            ["bash", "install.sh", "--version", "main"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode != 0
        assert "only supports latest, stable, or release versions" in result.stderr
        assert "scripts/install_source.sh" in result.stderr
    assert "only supports latest, stable, or release versions" in ps1
    assert "scripts/install_source.ps1" in ps1


def test_windows_installer_stops_when_native_install_command_fails() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")

    assert "$installExitCode = $LASTEXITCODE" in ps1
    assert 'if ($installExitCode -ne 0) {' in ps1
    assert "install_source.ps1: install command failed with exit code $installExitCode." in ps1
    assert (
        "Close any running OpenSquilla gateway or shell using the existing "
        "tool environment, then retry."
        in ps1
    )
    assert "exit $installExitCode" in ps1


def test_install_script_banners_are_ascii_for_windows_terminals() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        RELEASE_SH.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
        SOURCE_SH.read_text(encoding="utf-8"),
    ]

    for script in scripts:
        assert "OpenSquilla installed" in script
        assert "----" in script
        assert "→" not in script
        assert "─" not in script
        assert "⚠" not in script


def test_install_scripts_support_optional_extras() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        RELEASE_SH.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
        SOURCE_SH.read_text(encoding="utf-8"),
    ]

    for script in scripts:
        assert "OPENSQUILLA_INSTALL_EXTRAS" in script
        for legacy_extra in ("feishu", "telegram", "dingtalk", "wecom", "qq"):
            assert legacy_extra not in script
        assert "matrix" in script
        assert "matrix-e2e" in script
        assert "document-extras" in script
        assert "msteams" not in script


def test_windows_installer_bootstraps_vc_redist_for_router_runtime() -> None:
    scripts = [
        RELEASE_PS1.read_text(encoding="utf-8"),
        SOURCE_PS1.read_text(encoding="utf-8"),
    ]

    for ps1 in scripts:
        assert "Install-WindowsVCRedistIfNeeded" in ps1
        assert "OPENSQUILLA_SKIP_VC_REDIST" in ps1
        assert "Microsoft.VCRedist.2015+.x64" in ps1
        assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in ps1
        assert "safe router fallback" in ps1
        assert "If automatic installation fails, install it manually" in ps1
        assert "After installing, reopen PowerShell and restart OpenSquilla" in ps1


def test_source_install_pins_python_312_and_refuses_below() -> None:
    sh = SOURCE_SH.read_text(encoding="utf-8")
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    # uv provisions a known-good 3.12, never the ambient interpreter
    assert "--python 3.12" in sh
    assert "'--python', '3.12'" in ps1
    # the pip fallback refuses to install on python < 3.12 (no silent broken install)
    assert "sys.version_info >= (3, 12)" in sh
    assert "astral.sh/uv/install.sh" in sh
    # Windows pip fallback also gated; self-check targets code-task, not just --version
    assert "sys.version_info >= (3, 12)" in ps1
    assert "code-task --help" in sh


def test_source_installers_build_webui_and_keep_dry_run_non_mutating() -> None:
    sh = SOURCE_SH.read_text(encoding="utf-8")
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    required_node = (ROOT / "opensquilla-webui" / ".node-version").read_text(
        encoding="utf-8"
    ).strip()
    package = json.loads(
        (ROOT / "opensquilla-webui" / "package.json").read_text(encoding="utf-8")
    )
    assert package["engines"]["node"] == f">={required_node}"

    for script in (sh, ps1):
        assert ".node-version" in script
        assert required_node not in script
        assert "npm ci" in script
        assert "npm run build" in script
        assert "official wheel/Desktop installer" in script

    assert sh.index('if [[ "${dry_run}" = "1" ]]') < sh.index("build_webui\n")
    assert ps1.index("if ($dryRun) {") < ps1.index("Build-WebUI\n")
    assert "would run in ${webui_dir}: npm ci" in sh
    assert 'would run in ${webuiDir}: npm ci' in ps1


def test_source_installers_fail_closed_when_frontend_build_fails() -> None:
    sh = SOURCE_SH.read_text(encoding="utf-8")
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")

    assert "set -euo pipefail" in sh
    assert "npm ci\n        npm run build" in sh
    assert "npm ci failed with exit code" in ps1
    assert "npm run build failed with exit code" in ps1
    assert "PSNativeCommandUseErrorActionPreference" in ps1
    assert ps1.index("PSNativeCommandUseErrorActionPreference") < ps1.index(
        "function Build-WebUI"
    )
    assert "[Console]::Error.WriteLine" in ps1
    assert "exit $npmExitCode" in ps1
    assert ps1.index("Build-WebUI\n") < ps1.index(
        'Write-Host "install_source.ps1: installing via $installer'
    )


def test_source_shell_dry_run_does_not_execute_node_npm_or_installer(
    tmp_path: Path,
) -> None:
    if sys.platform.startswith("win"):
        return

    fake_bin = tmp_path / "bin"
    markers = tmp_path / "markers"
    fake_bin.mkdir()
    markers.mkdir()
    for command in ("node", "npm", "uv"):
        executable = fake_bin / command
        executable.write_text(
            f'#!/bin/sh\n: > "$FAKE_MARKER_DIR/{command}"\nexit 99\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "FAKE_MARKER_DIR": str(markers),
            "OPENSQUILLA_INSTALL_DRY_RUN": "1",
            "OPENSQUILLA_INSTALL_PROFILE": "core",
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    result = subprocess.run(
        ["bash", str(SOURCE_SH)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "would run in" in result.stdout
    assert list(markers.iterdir()) == []


def test_source_shell_npm_failure_prevents_python_install(tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        return

    fake_bin = tmp_path / "bin"
    markers = tmp_path / "markers"
    fake_bin.mkdir()
    markers.mkdir()
    node = fake_bin / "node"
    node.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo v22.12.0; fi\nexit 0\n',
        encoding="utf-8",
    )
    node.chmod(0o755)
    npm = fake_bin / "npm"
    npm.write_text(
        '#!/bin/sh\n: > "$FAKE_MARKER_DIR/npm"\nexit 17\n',
        encoding="utf-8",
    )
    npm.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text(
        '#!/bin/sh\n: > "$FAKE_MARKER_DIR/uv"\nexit 0\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "FAKE_MARKER_DIR": str(markers),
            "OPENSQUILLA_INSTALL_PROFILE": "core",
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    result = subprocess.run(
        ["bash", str(SOURCE_SH)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 17
    assert (markers / "npm").is_file()
    assert not (markers / "uv").exists()


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="PowerShell native exit-code propagation is a Windows installer contract.",
)
@pytest.mark.parametrize(
    ("npm_exit", "uv_exit", "expected_exit", "expected_error"),
    (
        (17, 0, 17, "npm ci failed with exit code 17"),
        (0, 23, 23, "install command failed with exit code 23"),
    ),
)
def test_source_powershell_preserves_native_failure_exit_codes(
    tmp_path: Path,
    npm_exit: int,
    uv_exit: int,
    expected_exit: int,
    expected_error: str,
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None, "Windows CI must provide a PowerShell host"

    fake_bin = tmp_path / "bin"
    markers = tmp_path / "markers"
    fake_bin.mkdir()
    markers.mkdir()
    (fake_bin / "node.cmd").write_text(
        "@echo off\r\n"
        'if "%~1"=="--version" echo v22.12.0\r\n'
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    (fake_bin / "npm.cmd").write_text(
        "@echo off\r\n"
        'type nul > "%FAKE_MARKER_DIR%\\npm"\r\n'
        "exit /b %FAKE_NPM_EXIT%\r\n",
        encoding="utf-8",
    )
    (fake_bin / "uv.cmd").write_text(
        "@echo off\r\n"
        'type nul > "%FAKE_MARKER_DIR%\\uv"\r\n'
        "exit /b %FAKE_UV_EXIT%\r\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "FAKE_MARKER_DIR": str(markers),
            "FAKE_NPM_EXIT": str(npm_exit),
            "FAKE_UV_EXIT": str(uv_exit),
            "OPENSQUILLA_INSTALL_PROFILE": "core",
            "OPENSQUILLA_PREFIX": str(tmp_path / "prefix"),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SOURCE_PS1),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert expected_error in result.stderr
    assert (markers / "npm").is_file()
    assert (markers / "uv").is_file() is (npm_exit == 0)


def test_source_shell_node_version_comparator_covers_stable_boundaries() -> None:
    node = shutil.which("node")
    if node is None:
        return

    sh = SOURCE_SH.read_text(encoding="utf-8")
    start = "    if ! node -e '\n"
    end = "\n    ' \"${minimum_node_version}\"; then"
    comparator = sh.split(start, 1)[1].split(end, 1)[0]

    cases = (
        ("22.11.99", "22.12.0", 1),
        ("22.12.0", "22.12.0", 0),
        ("22.12.1", "22.12.0", 0),
        ("23.0.0", "22.12.0", 0),
        ("21.99.99", "22.0.0", 1),
    )
    for installed, required, expected in cases:
        override = (
            "Object.defineProperty(process.versions, 'node', "
            f"{{ value: '{installed}' }});\n"
        )
        result = subprocess.run(
            [node, "-e", f"{override}{comparator}", required],
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == expected, (
            f"installed={installed}, required={required}: "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )


def test_windows_installer_verifies_entry_point_is_on_path() -> None:
    # Regression for #500: install_source.ps1 used to succeed silently and
    # leave `opensquilla` unresolvable on a fresh Windows host, because uv
    # drops entry points in ~/.local/bin (not on PATH by default). The POSIX
    # installer already smoke-checks this; the PowerShell installer must
    # reach parity by locating the entry point and warning when its dir is
    # not on PATH.
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")

    assert "function Resolve-EntrypointDir" in ps1
    assert "function Test-DirOnUserPath" in ps1
    assert "function Write-PathHint" in ps1
    # Invoked after a real install (dry-run exits before this point).
    assert "Write-PathHint\n" in ps1
    # Same probe install_source.sh uses to locate the uv bin dir.
    assert "uv tool dir --bin" in ps1
    # Recommended remediation, matching troubleshooting.md and quickstart.
    assert "uv tool update-shell" in ps1
    # Clear failure output when the dir is missing from PATH.
    assert "entry points are NOT on PATH" in ps1


def test_install_scripts_both_locate_entry_point_by_absolute_path() -> None:
    # Parity: both installers probe `uv tool dir --bin` instead of trusting
    # PATH, so a fresh install can be smoke-checked regardless of whether
    # the user's shell has been reconfigured yet.
    sh = SOURCE_SH.read_text(encoding="utf-8")
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    assert "uv tool dir --bin" in sh
    assert "uv tool dir --bin" in ps1
