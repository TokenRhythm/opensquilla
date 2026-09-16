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
CURRENT_RELEASE_TAG = "v0.5.4"


def test_source_install_scripts_force_refresh_local_uv_tool_package() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    sh = SOURCE_SH.read_text(encoding="utf-8")

    assert "'--force', '--reinstall-package', 'opensquilla'" in ps1
    assert "--force --reinstall-package opensquilla" in sh


def test_source_installers_freeze_full_commit_in_install_receipt() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")
    sh = SOURCE_SH.read_text(encoding="utf-8")

    assert "source_commit_id" in ps1
    assert "source_commit_id" in sh
    assert "rev-parse --verify HEAD" in ps1
    assert "rev-parse --verify HEAD" in sh
    assert "^[0-9a-f]{40}$" in ps1
    assert "^[0-9a-f]{40}$" in sh
    assert sh.index('if [[ "${dry_run}" = "1" ]]') < sh.index(
        'git -C "${source_root}" rev-parse --verify HEAD'
    )
    assert ps1.index("if ($dryRun) {") < ps1.index(
        "$gitCommand.Source -C $sourceRoot rev-parse --verify HEAD"
    )
    assert 'install_target="${source_root}' in sh
    assert 'local model_root="${source_root}/src/' in sh
    assert '"${sourceRoot}[$($targetExtras' in ps1
    assert "$modelRoot = Join-Path $sourceRoot" in ps1


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


def test_source_windows_installer_captures_node_exit_code_before_pipeline() -> None:
    ps1 = SOURCE_PS1.read_text(encoding="utf-8")

    node_probe = "$rawNodeVersion = & $nodeCommand.Source --version 2>$null"
    node_exit_capture = "$nodeExitCode = $LASTEXITCODE"
    node_output_normalization = "$rawNodeVersion = $rawNodeVersion | Select-Object -First 1"
    node_probe_check = "if ($nodeExitCode -ne 0 -or -not $rawNodeVersion) {"

    probe_index = ps1.index(node_probe)
    assert ps1.index(node_exit_capture, probe_index) > probe_index
    assert ps1.index(node_exit_capture, probe_index) < ps1.index(
        node_output_normalization, probe_index
    )
    assert ps1.index(node_probe_check, probe_index) > ps1.index(
        node_output_normalization, probe_index
    )
    assert "(& $nodeCommand.Source --version 2>$null | Select-Object" not in ps1


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
        cwd=tmp_path,
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


@pytest.fixture(params=["powershell", "pwsh"])
def windows_powershell(request: pytest.FixtureRequest) -> str:
    if not sys.platform.startswith("win"):
        pytest.skip("Exercises the native Windows source installer")
    executable = shutil.which(request.param)
    assert executable is not None, f"Windows CI must provide {request.param}"
    return executable


def test_source_powershell_parses_with_legacy_windows_ansi_encoding(
    windows_powershell: str,
) -> None:
    # Windows PowerShell 5.1 loads BOM-less scripts using the system ANSI code page.
    # On CP1252, a UTF-8 em dash introduces a smart quote and breaks string parsing.
    env = os.environ.copy()
    env["SOURCE_INSTALLER_TO_PARSE"] = str(SOURCE_PS1)
    result = subprocess.run(
        [
            windows_powershell,
            "-NoProfile",
            "-Command",
            "& { $ErrorActionPreference = 'Stop'; "
            "$bytes = [IO.File]::ReadAllBytes($env:SOURCE_INSTALLER_TO_PARSE); "
            "$source = [Text.Encoding]::GetEncoding(1252).GetString($bytes); "
            "$tokens = $null; $parseErrors = $null; "
            "[Management.Automation.Language.Parser]::ParseInput("
            "$source, [ref]$tokens, [ref]$parseErrors) | Out-Null; "
            "if ($parseErrors.Count -gt 0) { "
            "$parseErrors | ForEach-Object { "
            "[Console]::Error.WriteLine(($_.ErrorId + ': ' + $_.Message)) }; exit 1 }; "
            "exit 0 }",
        ],
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _tool_environment(tmp_path: Path) -> Path:
    return tmp_path / "custom tools 中文" / "opensquilla"


def _process_row(path: str | Path | None, *, name: str = "python.exe") -> dict:
    return {
        "Name": name,
        "ProcessId": 4242,
        "ExecutablePath": str(path) if path is not None else None,
        "CommandLine": "private-command-line-must-not-be-printed",
    }


def _run_source_powershell(
    tmp_path: Path,
    powershell: str,
    *,
    rows: list[dict] | None = None,
    second_rows: list[dict] | None = None,
    fallback: dict | None = None,
    directory_exit: int = 0,
    cim_failure: bool = False,
    existing_environment: bool = True,
    dry_run: bool = False,
    pip: bool = False,
    legacy_encoding: bool = False,
    npm_exit: int = 0,
    install_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the actual script; fake only external commands and process snapshots."""
    fake_bin = tmp_path / "bin"
    markers = tmp_path / "markers"
    fake_bin.mkdir()
    markers.mkdir()
    target = _tool_environment(tmp_path)
    if existing_environment:
        (target / "Scripts").mkdir(parents=True)
        (target / "Scripts" / "python.exe").write_bytes(b"existing executable")
        (target / "old-package.txt").write_bytes(b"existing package")
    (fake_bin / "node.cmd").write_text(
        '@echo off\nif "%~1"=="--version" echo v22.12.0\nexit /b 0\n',
        encoding="utf-8",
    )
    (fake_bin / "npm.cmd").write_text(
        '@echo off\necho npm-%~1>> "%FAKE_MARKER_DIR%\\events"\n'
        'type nul > "%FAKE_MARKER_DIR%\\npm"\nexit /b %FAKE_NPM_EXIT%\n',
        encoding="utf-8",
    )
    (fake_bin / "python.cmd").write_text(
        '@echo off\nif not "%~1"=="-m" exit /b 0\n'
        'echo pip-install>> "%FAKE_MARKER_DIR%\\events"\n'
        'type nul > "%FAKE_MARKER_DIR%\\pip"\nexit /b %FAKE_INSTALL_EXIT%\n',
        encoding="utf-8",
    )
    directory_command = "echo %UV_TOOL_DIR%\n"
    if legacy_encoding:
        raw_directory = tmp_path / "uv-directory.ps1"
        raw_directory.write_text(
            '$bytes = [Text.Encoding]::UTF8.GetBytes($env:UV_TOOL_DIR + "`n")\n'
            "$stream = [Console]::OpenStandardOutput()\n"
            "$stream.Write($bytes, 0, $bytes.Length)\n$stream.Flush()\n",
            encoding="utf-8",
        )
        directory_command = (
            f'"{powershell}" -NoProfile -ExecutionPolicy Bypass -File "{raw_directory}"\n'
        )
    (fake_bin / "uv.cmd").write_text(
        "@echo off\n"
        + ("" if legacy_encoding else "chcp 65001 > nul\n")
        + 'if "%~1 %~2"=="tool dir" goto directory\n'
        'echo uv-install>> "%FAKE_MARKER_DIR%\\events"\n'
        'type nul > "%FAKE_MARKER_DIR%\\uv"\nexit /b %FAKE_INSTALL_EXIT%\n'
        ':directory\nif "%~3"=="--bin" exit /b 0\n'
        'echo uv-directory>> "%FAKE_MARKER_DIR%\\events"\n'
        'if not "%FAKE_DIRECTORY_EXIT%"=="0" exit /b %FAKE_DIRECTORY_EXIT%\n'
        + directory_command
        + "exit /b 0\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "processes.json"
    fixture.write_text(
        json.dumps(
            {
                "First": rows or [],
                "Second": second_rows if second_rows is not None else rows or [],
                "Fallback": fallback,
                "CimFailure": cim_failure,
                "Pip": pip,
            }
        ),
        encoding="utf-8",
    )
    wrapper = tmp_path / "run-installer.ps1"
    wrapper.write_text(
        r"""
$global:fixture = Get-Content -LiteralPath $env:FAKE_PROCESS_FIXTURE -Raw | ConvertFrom-Json
$global:scanCount = 0
[Console]::OutputEncoding = [Text.Encoding]::GetEncoding([int]$env:FAKE_OUTPUT_CODEPAGE)
$OutputEncoding = [Console]::OutputEncoding
function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName, [string[]]$Property, [uint32]$OperationTimeoutSec)
    $global:scanCount++
    [IO.File]::AppendAllText((Join-Path $env:FAKE_MARKER_DIR 'events'), "scan`n")
    [Console]::OutputEncoding.CodePage | Set-Content (Join-Path $env:FAKE_MARKER_DIR 'codepage')
    $PSBoundParameters | ConvertTo-Json | Set-Content (Join-Path $env:FAKE_MARKER_DIR 'cim.json')
    if ($global:fixture.CimFailure) { throw 'simulated CIM query failure' }
    if ($global:scanCount -eq 1) { return $global:fixture.First }
    return $global:fixture.Second
}
function Get-Process {
    [CmdletBinding()]
    param([int]$Id)
    [IO.File]::AppendAllText((Join-Path $env:FAKE_MARKER_DIR 'events'), "fallback-$Id`n")
    if ($null -eq $global:fixture.Fallback) { throw 'simulated inaccessible process' }
    if ($global:fixture.Fallback.PSObject.Properties.Name -contains 'Disappeared') {
        Write-Error 'process exited' -ErrorId NoProcessFoundForGivenId -ErrorAction Stop
    }
    return $global:fixture.Fallback
}
if ($global:fixture.Pip) {
    function Get-Command {
        [CmdletBinding()]
        param([string]$Name)
        if ($Name -eq 'uv') { return }
        Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
    }
}
$ErrorActionPreference = 'Stop'
& $env:FAKE_SOURCE_INSTALLER
exit $LASTEXITCODE
""",
        encoding="utf-8-sig",
    )
    env = os.environ.copy()
    env.update(
        {
            "FAKE_SOURCE_INSTALLER": str(SOURCE_PS1),
            "FAKE_PROCESS_FIXTURE": str(fixture),
            "FAKE_MARKER_DIR": str(markers),
            "FAKE_DIRECTORY_EXIT": str(directory_exit),
            "FAKE_NPM_EXIT": str(npm_exit),
            "FAKE_INSTALL_EXIT": str(install_exit),
            "FAKE_OUTPUT_CODEPAGE": "437" if legacy_encoding else "65001",
            "OPENSQUILLA_INSTALL_DRY_RUN": "1" if dry_run else "0",
            "OPENSQUILLA_INSTALL_PROFILE": "core",
            "OPENSQUILLA_INSTALL_EXTRAS": "",
            "OPENSQUILLA_STATE_DIR": str(tmp_path / "profile"),
            # Deliberately differs from UV_TOOL_DIR: this is not the uv environment.
            "OPENSQUILLA_PREFIX": str(tmp_path / "prefix"),
            "UV_TOOL_DIR": str(target.parent),
            "PATH": os.pathsep.join((str(fake_bin), env["PATH"])),
        }
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return result, markers


def _installer_events(markers: Path) -> list[str]:
    event_file = markers / "events"
    return event_file.read_text(encoding="utf-8").splitlines() if event_file.exists() else []


@pytest.mark.parametrize(
    ("npm_exit", "uv_exit", "expected_exit", "expected_error"),
    (
        (17, 0, 17, "npm ci failed with exit code 17"),
        (0, 23, 23, "install command failed with exit code 23"),
    ),
)
def test_source_powershell_preserves_native_failure_exit_codes(
    tmp_path: Path,
    windows_powershell: str,
    npm_exit: int,
    uv_exit: int,
    expected_exit: int,
    expected_error: str,
) -> None:
    result, markers = _run_source_powershell(
        tmp_path,
        windows_powershell,
        npm_exit=npm_exit,
        install_exit=uv_exit,
    )

    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert expected_error in result.stderr
    assert (markers / "npm").is_file()
    assert (markers / "uv").is_file() is (npm_exit == 0)


@pytest.mark.parametrize("identity", ["direct", "normalized", "fallback"])
def test_source_powershell_blocks_confirmed_tool_process_before_build(
    tmp_path: Path,
    windows_powershell: str,
    identity: str,
) -> None:
    target = _tool_environment(tmp_path)
    executable = target / "Scripts" / "python.exe"
    path = str(executable)
    fallback = None
    if identity == "normalized":
        path = str(target / "Scripts" / ".." / "Scripts" / "python.exe").upper()
        path = path.replace("\\", "/")
    if identity == "fallback":
        fallback = {"Path": path, "HasExited": False}
        path = None
    result, markers = _run_source_powershell(
        tmp_path,
        windows_powershell,
        rows=[_process_row(path)],
        fallback=fallback,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert str(target) in output
    assert "python.exe (PID 4242)" in output
    assert "Stop these processes and retry" in output
    assert "private-command-line" not in output
    assert "could not complete the uv environment process check" not in output
    assert not (markers / "npm").exists()
    assert not (markers / "uv").exists()
    assert executable.read_bytes() == b"existing executable"
    assert (target / "old-package.txt").read_bytes() == b"existing package"
    query = json.loads((markers / "cim.json").read_text(encoding="utf-8-sig"))
    assert query["ClassName"] == "Win32_Process"
    assert set(query["Property"]) == {"ProcessId", "Name", "ExecutablePath"}
    assert query["OperationTimeoutSec"] == 5
    assert query["ErrorAction"] == 1  # ActionPreference.Stop


def test_source_powershell_ignores_other_environments_and_unrelated_unreadable_processes(
    tmp_path: Path,
    windows_powershell: str,
) -> None:
    target = _tool_environment(tmp_path)
    rows = [
        _process_row(tmp_path / "Desktop" / "resources" / "python.exe"),
        _process_row(tmp_path / "other-uv" / "opensquilla" / "Scripts" / "python.exe"),
        _process_row(target.parent / "opensquilla-other" / "Scripts" / "python.exe"),
        _process_row(target / ".." / "other" / "Scripts" / "python.exe"),
        _process_row(tmp_path / "unrelated-python" / "python.exe"),
        _process_row(None, name="System"),
        _process_row(None, name="unrelated-service.exe"),
    ]
    result, markers = _run_source_powershell(tmp_path, windows_powershell, rows=rows)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (markers / "uv").is_file()
    assert "could not complete the uv environment process check" not in output
    assert "fallback-4242" not in _installer_events(markers)
    assert _installer_events(markers) == [
        "uv-directory",
        "scan",
        "npm-ci",
        "npm-run",
        "uv-directory",
        "scan",
        "uv-install",
    ]


def test_source_powershell_rechecks_after_build_before_replacing_tool(
    tmp_path: Path,
    windows_powershell: str,
) -> None:
    target = _tool_environment(tmp_path)
    result, markers = _run_source_powershell(
        tmp_path,
        windows_powershell,
        second_rows=[_process_row(target / "Scripts" / "python.exe")],
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Stop these processes and retry" in output
    assert _installer_events(markers) == [
        "uv-directory",
        "scan",
        "npm-ci",
        "npm-run",
        "uv-directory",
        "scan",
    ]
    assert not (markers / "uv").exists()
    assert (target / "old-package.txt").read_bytes() == b"existing package"


def test_source_powershell_reads_utf8_uv_directory_and_restores_legacy_console_encoding(
    tmp_path: Path,
    windows_powershell: str,
) -> None:
    target = _tool_environment(tmp_path)
    result, markers = _run_source_powershell(
        tmp_path,
        windows_powershell,
        rows=[_process_row(target / "Scripts" / "python.exe")],
        legacy_encoding=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Stop these processes and retry" in output
    assert not (markers / "uv").exists()
    assert (markers / "codepage").read_text(encoding="utf-8-sig").strip() == "437"


@pytest.mark.parametrize("failure", ["directory", "cim", "candidate", "empty-path"])
@pytest.mark.parametrize("install_exit", [0, 23])
def test_source_powershell_warns_once_and_preserves_install_result_when_check_incomplete(
    tmp_path: Path,
    windows_powershell: str,
    failure: str,
    install_exit: int,
) -> None:
    result, markers = _run_source_powershell(
        tmp_path,
        windows_powershell,
        directory_exit=19 if failure == "directory" else 0,
        cim_failure=failure == "cim",
        rows=[_process_row(None)] if failure in {"candidate", "empty-path"} else [],
        fallback={"Path": None, "HasExited": False} if failure == "empty-path" else None,
        install_exit=install_exit,
    )

    output = result.stdout + result.stderr
    assert result.returncode == install_exit, output
    assert output.count("could not complete the uv environment process check; continuing") == 1
    assert "Stop these processes and retry" not in output
    assert (markers / "uv").is_file()
    assert _installer_events(markers).count("uv-directory") == 2
    if install_exit:
        assert f"install command failed with exit code {install_exit}" in output


@pytest.mark.parametrize("fallback_identity", ["outside", "exited", "disappeared"])
def test_source_powershell_does_not_block_or_warn_for_resolved_non_target_candidates(
    tmp_path: Path,
    windows_powershell: str,
    fallback_identity: str,
) -> None:
    target = _tool_environment(tmp_path)
    fallback = {
        "Path": str(
            target / "Scripts" / "python.exe"
            if fallback_identity == "exited"
            else tmp_path / "another-tool" / "python.exe"
        ),
        "HasExited": fallback_identity == "exited",
    }
    if fallback_identity == "disappeared":
        fallback["Disappeared"] = True
    result, markers = _run_source_powershell(
        tmp_path,
        windows_powershell,
        rows=[_process_row(None)],
        fallback=fallback,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "could not complete the uv environment process check" not in output
    assert (markers / "uv").is_file()
    assert _installer_events(markers).count("fallback-4242") == 2


@pytest.mark.parametrize("route", ["first-install", "idle-upgrade", "dry-run", "pip"])
def test_source_powershell_preserves_normal_install_routes(
    tmp_path: Path,
    windows_powershell: str,
    route: str,
) -> None:
    result, markers = _run_source_powershell(
        tmp_path,
        windows_powershell,
        existing_environment=route != "first-install",
        dry_run=route == "dry-run",
        pip=route == "pip",
        # A broken query proves skipped branches never inspect the process table.
        cim_failure=route != "idle-upgrade",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "could not complete the uv environment process check" not in output
    events = _installer_events(markers)
    if route == "dry-run":
        assert events == []
        assert "would check the uv tool environment" in output
    elif route == "pip":
        assert events == ["npm-ci", "npm-run", "pip-install"]
        assert (markers / "pip").is_file()
    else:
        assert events.count("scan") == (2 if route == "idle-upgrade" else 0)
        assert events.count("uv-directory") == 2
        assert (markers / "uv").is_file()


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


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("repository", [None, "opensquilla/opensquilla", "example/custom-runtime"])
def test_release_installer_repository_defaults_and_overrides(
    shell: str,
    repository: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shell == "bash":
        if sys.platform.startswith("win"):
            pytest.skip("Bash release installer targets Linux and macOS")
        executable = shutil.which("bash")
        arguments = [str(RELEASE_SH), "--version", CURRENT_RELEASE_TAG, "--profile", "core"]
    else:
        executable = shutil.which("pwsh") or shutil.which("powershell")
        arguments = [
            "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(RELEASE_PS1),
            "-Version", CURRENT_RELEASE_TAG, "-Profile", "core",
        ]
    if executable is None:
        pytest.skip(f"{shell} is not installed")

    monkeypatch.setenv("OPENSQUILLA_INSTALL_DRY_RUN", "1")
    monkeypatch.delenv("OPENSQUILLA_INSTALL_EXTRAS", raising=False)
    if repository is None:
        monkeypatch.delenv("OPENSQUILLA_REPOSITORY", raising=False)
    else:
        monkeypatch.setenv("OPENSQUILLA_REPOSITORY", repository)

    result = subprocess.run(
        [executable, *arguments], cwd=tmp_path, capture_output=True, text=True, check=False,
    )

    expected_repository = repository or "TokenRhythm/opensquilla"
    release_version = CURRENT_RELEASE_TAG.removeprefix("v")
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert (
        f"https://github.com/{expected_repository}/releases/download/{CURRENT_RELEASE_TAG}/"
        f"opensquilla-{release_version}-py3-none-any.whl"
    ) in result.stdout
