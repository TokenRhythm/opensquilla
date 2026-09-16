"""Exercise the Windows UX harness with synthetic process and command boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# PowerShell cold starts and native process trees share the hosted runner's
# process budget. Keep their deadlines independent of the parallel worker pool.
pytestmark = pytest.mark.ci_serial

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test_gateway_ux.ps1"


@pytest.fixture(params=["powershell", "pwsh"] if sys.platform == "win32" else ["pwsh"])
def powershell(request: pytest.FixtureRequest) -> str:
    executable = shutil.which(request.param)
    if sys.platform == "win32":
        assert executable, f"Windows CI must provide {request.param}"
    if not executable:
        pytest.skip("PowerShell is required to execute the Windows harness")
    return executable


def run_harness(tmp_path: Path, powershell: str, scenario: str) -> dict:
    wrapper = tmp_path / "exercise.ps1"
    wrapper.write_text(
        r"""
$ErrorActionPreference = 'Stop'
$global:starts = @()
$global:kills = @()
$global:live = @{}
$env:ComSpec = 'fixture-cmd.exe'
$env:OPENSQUILLA_GATEWAY_URL = 'http://localhost:12345'
$env:OPENSQUILLA_STATE_DIR = 'original-state'
$env:OPENSQUILLA_USER_STATE_DIR = 'original-user-state'
$env:OPENSQUILLA_LISTEN = 'original-listen'
function Get-Command($Name) {
  if ($Name -eq 'npm.cmd') { return @{ Source = 'C:\Fixture Tools\npm.cmd' } }
  return @{ Source = $Name }
}
function Start-Process {
  param($FilePath, $ArgumentList, $WorkingDirectory, $RedirectStandardOutput,
        $RedirectStandardError, [switch]$PassThru)
  if ($env:UX_SCENARIO -eq 'launch-failure' -and $global:starts.Count -eq 1) {
    throw 'synthetic launch failure'
  }
  $id = 4101 + $global:starts.Count
  $global:starts += @{
    file = $FilePath; arguments = $ArgumentList
    gatewayUrl = $env:OPENSQUILLA_GATEWAY_URL
    state = $env:OPENSQUILLA_STATE_DIR
    userState = $env:OPENSQUILLA_USER_STATE_DIR
  }
  $process = [pscustomobject]@{ Id = $id; StartTime = [DateTime]'2025-01-01T00:00:00Z' }
  $global:live[$id] = $process
  return $process
}
function Get-Process($Id, $ErrorAction) { return $global:live[[int]$Id] }
function taskkill.exe {
  $global:kills += ,@($args)
  $global:LASTEXITCODE = 0
  if ($env:UX_SCENARIO -eq 'kill-failure') { $global:LASTEXITCODE = 1; return }
  $global:live.Remove([int]$args[1])
}
$failure = $null
$duplicateFailure = $null
try {
  & $env:UX_SCRIPT Start -GatewayPort 19871 -WebPort 19872
  if ($env:UX_SCENARIO -eq 'lifecycle') {
    try { & $env:UX_SCRIPT Start } catch { $duplicateFailure = $_.Exception.Message }
    & $env:UX_SCRIPT Disconnect
    $liveAfterDisconnect = @($global:live.Keys)
    & $env:UX_SCRIPT Stop
  } elseif ($env:UX_SCENARIO -eq 'pid-reuse') {
    $global:live[4101].StartTime = [DateTime]'2025-01-02T00:00:00Z'
    & $env:UX_SCRIPT Stop
  } elseif ($env:UX_SCENARIO -eq 'kill-failure') {
    & $env:UX_SCRIPT Stop
  }
} catch { $failure = $_.Exception.Message }
$pidFile = Join-Path ([IO.Path]::GetTempPath()) 'opensquilla-gateway-ux/pids.json'
@{
  starts = @($global:starts); kills = @($global:kills)
  liveAfterDisconnect = @($liveAfterDisconnect); live = @($global:live.Keys)
  failure = $failure; duplicateFailure = $duplicateFailure
  pidFileExists = (Test-Path $pidFile)
  restored = @($env:OPENSQUILLA_GATEWAY_URL, $env:OPENSQUILLA_STATE_DIR,
               $env:OPENSQUILLA_USER_STATE_DIR, $env:OPENSQUILLA_LISTEN)
} | ConvertTo-Json -Depth 8 -Compress | Set-Content $env:UX_RESULT
""",
        encoding="utf-8",
    )
    result_path = tmp_path / "result.json"
    env = {
        **os.environ,
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "UX_SCRIPT": str(SCRIPT),
        "UX_RESULT": str(result_path),
        "UX_SCENARIO": scenario,
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["restored"] == [
        "http://localhost:12345",
        "original-state",
        "original-user-state",
        "original-listen",
    ]
    return payload


def test_gateway_ux_connects_to_its_gateway_and_stops_both_trees(
    tmp_path: Path, powershell: str
) -> None:
    result = run_harness(tmp_path, powershell, "lifecycle")
    assert result["failure"] is None
    assert "already recorded" in result["duplicateFailure"]
    gateway, web = result["starts"]
    assert gateway["arguments"][-2:] == ["--port", "19871"]
    assert web["gatewayUrl"] == "http://127.0.0.1:19871"
    assert Path(web["state"]) == tmp_path / "opensquilla-gateway-ux/state"
    assert Path(web["userState"]) == tmp_path / "opensquilla-gateway-ux/user-state"
    assert web["file"] == "fixture-cmd.exe"
    assert web["arguments"] == [
        "/d", "/s", "/c",
        '""C:\\Fixture Tools\\npm.cmd" run dev -- --host 127.0.0.1 --port 19872 --strictPort"',
    ]
    assert result["liveAfterDisconnect"] == [4102]
    assert result["kills"] == [["/PID", 4101, "/T", "/F"], ["/PID", 4102, "/T", "/F"]]
    assert result["live"] == []
    assert not result["pidFileExists"]


def test_gateway_ux_cleans_gateway_after_web_launch_failure(
    tmp_path: Path, powershell: str
) -> None:
    result = run_harness(tmp_path, powershell, "launch-failure")
    assert result["failure"] == "synthetic launch failure"
    assert result["kills"] == [["/PID", 4101, "/T", "/F"]]
    assert result["live"] == []
    assert not result["pidFileExists"]


@pytest.mark.parametrize("scenario", ["pid-reuse", "kill-failure"])
def test_gateway_ux_retains_process_records_when_stop_is_not_safe(
    tmp_path: Path, powershell: str, scenario: str
) -> None:
    result = run_harness(tmp_path, powershell, scenario)
    assert result["pidFileExists"]
    assert sorted(result["live"]) == [4101, 4102]
    if scenario == "pid-reuse":
        assert "different process" in result["failure"]
        assert result["kills"] == []
    else:
        assert "taskkill exit 1" in result["failure"]


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows process trees")
def test_gateway_ux_disconnect_terminates_native_children(tmp_path: Path, powershell: str) -> None:
    wrapper = tmp_path / "native-trees.ps1"
    wrapper.write_text(
        r"""
$ErrorActionPreference = 'Stop'
$runner = (Get-Process -Id $PID).Path
$parentSource = @'
$runner = (Get-Process -Id $PID).Path
$childArgs = @('-NoProfile', '-Command', 'Start-Sleep 120')
$child = Start-Process $runner -ArgumentList $childArgs -PassThru
$child.Id | Set-Content $env:UX_CHILD_PID_FILE
Start-Sleep 120
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($parentSource))
$parents = @()
$children = @()
try {
  foreach ($name in @('gateway', 'web')) {
    $env:UX_CHILD_PID_FILE = Join-Path $env:UX_TEST_ROOT "$name.child"
    $parentArgs = @('-NoProfile', '-EncodedCommand', $encoded)
    $parent = Start-Process $runner -ArgumentList $parentArgs -PassThru
    $parents += $parent
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    $childId = 0
    while (-not $childId) {
      if ([DateTime]::UtcNow -gt $deadline) { throw 'Child startup timed out' }
      if (Test-Path $env:UX_CHILD_PID_FILE) {
        [int]::TryParse((Get-Content $env:UX_CHILD_PID_FILE -Raw), [ref]$childId) | Out-Null
      }
      Start-Sleep -Milliseconds 50
    }
    $children += $childId
  }
  $root = Join-Path ([IO.Path]::GetTempPath()) 'opensquilla-gateway-ux'
  New-Item -ItemType Directory -Force $root | Out-Null
  @{
    gateway = @{
      id = $parents[0].Id; startedAtTicks = $parents[0].StartTime.ToUniversalTime().Ticks
    }
    web = @{
      id = $parents[1].Id; startedAtTicks = $parents[1].StartTime.ToUniversalTime().Ticks
    }
  } | ConvertTo-Json | Set-Content (Join-Path $root 'pids.json')
  & $env:UX_SCRIPT Disconnect
  $deadline = [DateTime]::UtcNow.AddSeconds(5)
  while (Get-Process -Id $children[0] -ErrorAction SilentlyContinue) {
    if ([DateTime]::UtcNow -gt $deadline) { throw 'Gateway child survived disconnect' }
    Start-Sleep -Milliseconds 50
  }
  if (-not (Get-Process -Id $children[1] -ErrorAction SilentlyContinue)) {
    throw 'Disconnect terminated the WebUI'
  }
  & $env:UX_SCRIPT Stop
  $deadline = [DateTime]::UtcNow.AddSeconds(5)
  while (Get-Process -Id $children[1] -ErrorAction SilentlyContinue) {
    if ([DateTime]::UtcNow -gt $deadline) { throw 'WebUI child survived stop' }
    Start-Sleep -Milliseconds 50
  }
} finally {
  foreach ($process in $parents) {
    if (-not $process.HasExited) { & taskkill.exe /PID $process.Id /T /F | Out-Null }
  }
  foreach ($id in $children) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        env={
            **os.environ,
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
            "UX_SCRIPT": str(SCRIPT),
            "UX_TEST_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
