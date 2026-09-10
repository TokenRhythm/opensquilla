from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / ".github/scripts/verify-release-windows-signed-update.ps1"
WRITE_VIEW_SPEC = importlib.util.spec_from_file_location(
    "native_audit_write_view", ROOT / ".github/scripts/native-audit-write-view.py"
)
assert WRITE_VIEW_SPEC and WRITE_VIEW_SPEC.loader
WRITE_VIEW = importlib.util.module_from_spec(WRITE_VIEW_SPEC)
WRITE_VIEW_SPEC.loader.exec_module(WRITE_VIEW)


def _powershell(script: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell is required for the signed audit contracts")
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-File", str(script)],
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_signed_audit_powershell_parses_without_executing(tmp_path: Path) -> None:
    parser = tmp_path / "parse.ps1"
    parser.write_text(
        "$tokens = $null; $errors = $null\n"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:AUDIT_SCRIPT, [ref]$tokens, [ref]$errors) | Out-Null\n"
        "if ($errors.Count) { throw ($errors | Out-String) }\n",
        encoding="utf-8",
    )
    result = _powershell(parser, AUDIT_SCRIPT=str(AUDIT))
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_profile_matches_the_packaged_electron_name(tmp_path: Path) -> None:
    # NSIS productName is OpenSquilla, but Electron's default userData follows
    # its scoped package name. A brand-named test profile is not inherited by
    # the ordinary Finish/Run launch.
    package = json.loads((ROOT / "desktop/electron/package.json").read_text(encoding="utf-8"))
    roaming = tmp_path.resolve() / "Roaming with spaces 中文"
    script = tmp_path / "native-profile.ps1"
    script.write_text(
        ". $env:AUDIT_SCRIPT\n"
        "Get-SignedAuditNativeUserData $env:AUDIT_ROAMING_ROOT\n",
        encoding="utf-8",
    )
    result = _powershell(script, AUDIT_SCRIPT=str(AUDIT), AUDIT_ROAMING_ROOT=str(roaming))
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(result.stdout.strip()) == roaming / package["name"]
    assert Path(result.stdout.strip()) != roaming / "OpenSquilla"
    assert not roaming.exists(), "Resolving the default profile must not create it"


@pytest.fixture
def audit_harness(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Run the real orchestration with OS/process boundaries replaced in a copy.

    No product bypass is added. No Electron, installer, signing, real profile,
    or process kill is run; the native gate needs separate operator evidence.
    """
    # Hosted Windows runners may expose Temp through a short-name alias. Resolve
    # the fixture itself; do not weaken the real native-path preflight.
    tmp_path = tmp_path.resolve()
    repo = tmp_path / "repo"
    scripts = repo / ".github/scripts"
    scripts.mkdir(parents=True)
    node_executable = shutil.which("node")
    if not node_executable:
        pytest.skip("Node is required for the real read-only native-path preflight")
    source = AUDIT.read_text(encoding="utf-8")
    replacements = {
        "if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT)": "if ($false)",
        "([Diagnostics.FileVersionInfo]::GetVersionInfo($plan.Executable)).ProductVersion": (
            "$global:SyntheticVersion"
        ),
    }
    for original, replacement in replacements.items():
        assert original in source
        source = source.replace(original, replacement)
    (scripts / AUDIT.name).write_text(source, encoding="utf-8")
    (scripts / "verify-windows-signatures.ps1").write_text(
        "param($InstallerPath, $InstalledRoot)\n"
        "$global:SignatureCalls += 1\n"
        '$global:Calls.Add("signature-$global:SignatureCalls")\n'
        '$global:LASTEXITCODE = if ($env:AUDIT_FAIL -eq "signature-$global:SignatureCalls") '
        "{ 1 } else { 0 }\n",
        encoding="utf-8",
    )
    install = tmp_path / "installed-A"
    install.mkdir()
    (install / "OpenSquilla.exe").write_bytes(b"synthetic installed A")
    candidate = tmp_path / "OpenSquilla-0.5.7-win-x64.exe"
    candidate.write_bytes(b"synthetic candidate B")
    manifest = tmp_path / "channel.json"
    manifest.write_text(
        json.dumps({"schemaVersion": 1, "version": "0.5.7", "tag": "v0.5.7", "prerelease": False}),
        encoding="utf-8",
    )
    roaming = tmp_path / "native-roaming"
    profile = roaming / "@opensquilla" / "desktop-electron"
    profile.parent.mkdir(parents=True)
    evidence = tmp_path / "evidence"
    options = {
        "InstallRoot": str(install),
        "UserDataDir": str(profile),
        "EvidenceRoot": str(evidence),
        "BaselineVersion": "0.5.6",
        "BaselineExecutableSha256": hashlib.sha256(b"synthetic installed A").hexdigest(),
        "BaselineSourceSha": "a" * 40,
        "CandidateInstaller": str(candidate),
        "CandidateInstallerSha256": hashlib.sha256(b"synthetic candidate B").hexdigest(),
        "CandidateSourceSha": "b" * 40,
        "ChannelManifest": str(manifest),
        "InstallTimeoutSeconds": 1,
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(options), encoding="utf-8")
    runner = tmp_path / "harness.ps1"
    runner.write_text(
        r"""
$ErrorActionPreference = 'Stop'
. $env:AUDIT_SCRIPT
$options = @{}
(Get-Content -Raw $env:AUDIT_INPUT | ConvertFrom-Json).PSObject.Properties |
  ForEach-Object { $options[$_.Name] = $_.Value }
$global:Calls = [Collections.Generic.List[string]]::new()
$global:SignatureCalls = 0
$global:SyntheticVersion = $options.BaselineVersion
$global:TargetExecutable = Join-Path $options.InstallRoot 'OpenSquilla.exe'
$global:CreatedAt = [datetime]::UtcNow.AddSeconds(-1)
$global:QuitObserved = $false
$global:PreflightNodeCalls = 0
function Get-SignedAuditLauncherPackageStatus {
  if ($env:AUDIT_PACKAGE_STATUS -eq 'unavailable') { throw 'Synthetic package API unavailable.' }
  if ($env:AUDIT_PACKAGE_STATUS) { return [int]$env:AUDIT_PACKAGE_STATUS }
  return 15700
}
function Get-SignedAuditNodeExecutable { return 'node' }
function Get-SignedAuditPythonExecutable { return 'python' }
function Get-SignedAuditRoamingRoot { return $env:AUDIT_ROAMING_ROOT }
function Test-SignedAuditElevated { return $env:AUDIT_ELEVATED -eq '1' }
function Get-CimInstance {
  [CmdletBinding()] param($ClassName, $Filter)
  if ($Filter -eq "Name='OpenSquilla.exe'") {
    $global:Calls.Add('poll-own-processes')
    if ($env:AUDIT_FAIL -eq 'polling') { throw 'Synthetic CIM access denied.' }
    if ($global:SyntheticVersion -eq '0.5.7' -and -not $global:QuitObserved) {
      return [pscustomobject]@{
        ProcessId = 42; ParentProcessId = 9; CreationDate = $global:CreatedAt
        ExecutablePath = $global:TargetExecutable
        CommandLine = '"OpenSquilla.exe" "--updated"'
      }
    }
    return
  }
  if ($Filter -eq 'ProcessId=42' -and $global:QuitObserved) {
    if ($env:AUDIT_FAIL -eq 'quit-cim-error') {
      $global:Calls.Add('quit-cim-error')
      Write-Error 'Synthetic CIM query unavailable; the original B is still alive.'
      return
    }
    if ($env:AUDIT_FAIL -in @('quit-pid-reused', 'quit-still-alive')) {
      $global:Calls.Add($env:AUDIT_FAIL)
      $created = if ($env:AUDIT_FAIL -eq 'quit-pid-reused') {
        $global:CreatedAt.AddSeconds(1)
      } else { $global:CreatedAt }
      return [pscustomobject]@{
        ProcessId = 42; ExecutablePath = $global:TargetExecutable; CreationDate = $created
      }
    }
    return
  }
  if ($Filter -eq 'ProcessId=42') {
    $created = if ($env:AUDIT_FAIL -eq 'postinstall-pid-reused') {
      $global:CreatedAt.AddSeconds(1)
    } else { $global:CreatedAt }
    return [pscustomobject]@{
      ExecutablePath = $global:TargetExecutable; CreationDate = $created
    }
  }
}
function Register-CimIndicationEvent { param($Query, $SourceIdentifier, $MessageData, $Action)
  $global:Queue = $MessageData.Queue
  $global:Calls.Add('observe-before-click')
  if ($env:AUDIT_FAIL -eq 'subscription') { throw 'Synthetic trace access denied.' }
  return [pscustomobject]@{ Id = 1 }
}
function Unregister-Event { param($SourceIdentifier, $ErrorAction) }
function Get-Event { param($SourceIdentifier, $ErrorAction) }
function Remove-Job { param($Job, [switch]$Force, $ErrorAction) }
function Read-Host { param($Prompt)
  if ($Prompt -like 'Use the running B tray Quit*') {
    $global:Calls.Add('normal-quit')
    if ($env:AUDIT_FAIL -eq 'quit') { return 'Task Manager' }
    $global:QuitObserved = $true
    return 'QUIT'
  }
  $global:Calls.Add('finish-attestation')
  if ($env:AUDIT_FAIL -eq 'attestation') { return 'manual launch' }
  return 'FINISH-AUTOLAUNCH'
}
function taskkill.exe { throw 'The audit must never force-kill the upgraded client.' }
function Get-ArgumentValue($Values, $Name) {
  $index = [array]::IndexOf($Values, $Name)
  if ($index -lt 0) { throw "Missing required argument: $Name" }
  return $Values[$index + 1]
}
function node {
  $values = @($args)
  $name = [IO.Path]::GetFileName($values[0])
  $global:LASTEXITCODE = 0
  if ($name -eq '-e') {
    $global:PreflightNodeCalls += 1
    if ($env:AUDIT_FAIL -eq 'node-preflight') { $global:LASTEXITCODE = 1; return }
    & $env:AUDIT_NODE_EXECUTABLE @values
    return
  }
  if ($name -eq 'verify-windows-native-write-view.mjs') {
    $global:Calls.Add('write-view')
    if ($values[1] -cne $env:AUDIT_ROAMING_ROOT -or
        $values[2] -cne 'python') {
      throw 'Write-view probe must use the frozen Python and top-level Roaming root.'
    }
    $result = @{ nativeWriteViewVerified = $true
      cleanup = @(@{ removed = $true }, @{ removed = $true }); retainedPaths = @() }
    switch ($env:AUDIT_FAIL) {
      'write-view-redirected' { $result.nativeWriteViewVerified = $false; $global:LASTEXITCODE = 1 }
      'write-view-string' { $result.nativeWriteViewVerified = 'true' }
      'write-view-retained' { $result.retainedPaths = @('synthetic-owned-probe') }
      'write-view-no-cleanup' { $result.cleanup = @() }
      'write-view-incomplete-cleanup' { $result.cleanup[1].removed = $false }
      'write-view-cleanup-string' { $result.cleanup[1].removed = 'true' }
    }
    $result | ConvertTo-Json -Depth 5 -Compress
    return
  }
  if ($name -eq 'test-packaged-real-update-flow.mjs') {
    $global:Calls.Add('handoff')
    if ($env:AUDIT_FAIL -eq 'handoff') { $global:LASTEXITCODE = 1; return }
    $when = [datetime]::UtcNow.AddSeconds(-1)
    $global:CreatedAt = $when.AddMilliseconds(1)
    if ($env:AUDIT_FAIL -eq 'restart-before-handoff') {
      $global:CreatedAt = $when.AddSeconds(-1)
    }
    $credentialPath = Join-Path $options.UserDataDir 'desktop-credential.json'
    $credentialHash = (Get-FileHash -LiteralPath $credentialPath -Algorithm SHA256).Hash
    $handoffMode = Get-ArgumentValue $values '--mode'
    $cached = $handoffMode -eq 'signed-cached-handoff'
    $handoff = @{
      stage = 'installer-handoff'; handoffObserved = $true; requiresPostInstallVerification = $true
      ok = $false; fromVersion = $options.BaselineVersion; toVersion = '0.5.7'
      sha256 = $options.CandidateInstallerSha256; sourceSha = $options.CandidateSourceSha
      handoffStartedAt = $when.ToString('o'); oldPid = 41
      credentialSha256 = $credentialHash.ToLowerInvariant()
      mode = $handoffMode; inputMode = $(if ($cached) { 'verified-cache' } else { 'download' })
      downloadVerified = -not $cached; remotePublicationVerified = $false
    }
    if ($cached) {
      if ((Get-ArgumentValue $values '--cached-installer') -cne $options.CandidateInstaller -or
          (Get-ArgumentValue $values '--baseline-source-sha') -cne $options.BaselineSourceSha) {
        throw 'Cached driver arguments must bind the original source installer and A source.'
      }
      $cacheMarkerPath = Join-Path $options.UserDataDir 'cached-handoff-audit.json'
      $cacheMarker = Get-Content -LiteralPath $cacheMarkerPath -Raw | ConvertFrom-Json
      if ($cacheMarker.purpose -cne 'opensquilla-synthetic-cached-handoff-audit' -or
          $cacheMarker.expectedSha256 -cne $options.CandidateInstallerSha256 -or
          $cacheMarker.sourceSha -cne $options.CandidateSourceSha -or
          $cacheMarker.baselineSourceSha -cne $options.BaselineSourceSha) {
        throw 'Cached marker must bind this fresh synthetic audit.'
      }
      $handoff.fixtureSource = 'local Actions artifact and local channel fixture'
      $handoff.candidateValidation = 'production-parser-on-local-fixture'
      $handoff.baselineSourceSha = $options.BaselineSourceSha
      $handoff.auditId = $cacheMarker.auditId
      $markerHash = (Get-FileHash -LiteralPath $cacheMarkerPath -Algorithm SHA256).Hash
      $handoff.markerSha256 = $markerHash.ToLowerInvariant()
      $handoff.installerSha256 = $options.CandidateInstallerSha256
      $manifestHash = (Get-FileHash -LiteralPath $options.ChannelManifest -Algorithm SHA256).Hash
      $handoff.manifestSha256 = $manifestHash.ToLowerInvariant()
      $handoff.cacheStagedAndVerified = $true
      $handoff.cacheRestoreVerified = $true
      $handoff.cacheRestartVerified = $true
      switch ($env:AUDIT_FAIL) {
        'cache-mode' { $handoff.mode = 'signed-handoff' }
        'cache-download-claim' { $handoff.downloadVerified = $true }
        'cache-publication-claim' { $handoff.remotePublicationVerified = $true }
        'cache-numeric-download' { $handoff.downloadVerified = 0 }
        'cache-marker' { $handoff.markerSha256 = '0' * 64 }
        'cache-source' { $handoff.baselineSourceSha = '0' * 40 }
        'cache-manifest' { $handoff.manifestSha256 = '0' * 64 }
        'cache-no-restart' { $handoff.Remove('cacheRestartVerified') }
        'cache-string-restart' { $handoff.cacheRestartVerified = 'true' }
        'cache-not-staged' { $handoff.cacheStagedAndVerified = $false }
      }
    }
    if ($env:AUDIT_FAIL -eq 'handoff-missing-credential') { $handoff.Remove('credentialSha256') }
    if ($env:AUDIT_FAIL -eq 'handoff-stale-credential') { $handoff.credentialSha256 = '0' * 64 }
    $handoff | ConvertTo-Json | Set-Content (Get-ArgumentValue $values '--ready-output')
    $global:SyntheticVersion = '0.5.7'
    [IO.File]::WriteAllText($global:TargetExecutable, 'synthetic installed B')
    # A shell broker parent is valid observation, never machine causality proof.
    if ($global:Queue) { $global:Queue.Enqueue([pscustomobject]@{
      Pid = 42; ParentPid = 9; StartedAt = [datetime]::UtcNow; CreatedAt = $global:CreatedAt
      Path = $global:TargetExecutable; CommandLine = '"OpenSquilla.exe" "--updated"'
    }) }
    return
  }
  if ($name -eq 'test-packaged-first-send-renderer.mjs') {
    $global:Calls.Add('first-send-new-profile')
    $fresh = Get-ArgumentValue $values '--user-data-dir'
    if ($fresh -ne (Join-Path $options.EvidenceRoot 'first-send-new-profile') -or
        (Test-Path -LiteralPath $fresh)) {
      throw 'First-send must receive a new empty profile distinct from retained A/B profile.'
    }
    New-Item -ItemType Directory -Path $fresh | Out-Null
    if ($env:AUDIT_FAIL -eq 'first-send') { $global:LASTEXITCODE = 1 }
    if ($env:AUDIT_FAIL -eq 'credential-changed') {
      $credentialPath = Join-Path $options.UserDataDir 'desktop-credential.json'
      [IO.File]::WriteAllText($credentialPath, 'changed retained credential')
    }
    return
  }
  if ($name -eq 'test-packaged-retained-interaction.mjs') {
    $global:Calls.Add('retained-interaction')
    $markerPath = Get-ArgumentValue $values '--audit-manifest'
    $output = Get-ArgumentValue $values '--output-dir'
    if ($markerPath -cne (Join-Path $options.UserDataDir 'retained-interaction-audit.json') -or
        $output -cne (Join-Path $options.EvidenceRoot 'retained-interaction') -or
        (Test-Path -LiteralPath $output)) {
      throw 'Retained probe must bind the native retained profile and a fresh output directory.'
    }
    $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ($marker.schemaVersion -ne 1 -or
        $marker.purpose -cne 'opensquilla-synthetic-signed-update-audit' -or
        $marker.auditId -cnotmatch '^[0-9a-f]{32}$' -or
        $marker.seedLabel -cne 'signed-update-audit' -or
        $marker.userDataDir -cne $options.UserDataDir -or
        $marker.executablePath -cne $global:TargetExecutable -or
        $marker.expectedVersion -cne '0.5.7' -or
        $marker.sourceSha -cne $options.CandidateSourceSha -or
        $marker.externalSentinelsDir -cne (Join-Path $options.EvidenceRoot 'external-sentinels')) {
      throw 'Retained marker does not bind this installed B and retained profile.'
    }
    $boundFiles = @{
      executableSha256 = $global:TargetExecutable
      credentialSha256 = Join-Path $options.UserDataDir 'desktop-credential.json'
      configSha256 = Join-Path (Join-Path $options.UserDataDir 'opensquilla') 'config.toml'
    }
    foreach ($key in $boundFiles.Keys) {
      $hash = (Get-FileHash -LiteralPath $boundFiles[$key] -Algorithm SHA256).Hash
      if ($marker.$key -cne $hash.ToLowerInvariant()) {
        throw "Retained marker does not pin actual input bytes: $key"
      }
    }
    if ($env:AUDIT_FAIL -eq 'retained-missing-report') { return }
    New-Item -ItemType Directory -Path $output | Out-Null
    $markerHash = (Get-FileHash -LiteralPath $markerPath -Algorithm SHA256).Hash
    $report = @{
      schemaVersion = 1; ok = $true; status = 'passed'; auditId = $marker.auditId
      sourceSha = $marker.sourceSha; executableSha256 = $marker.executableSha256
      credentialSha256 = $marker.credentialSha256; configSha256 = $marker.configSha256
      markerSha256 = $markerHash.ToLowerInvariant()
      credentialPreserved = $true; configPreserved = $true; oldSessionsVerified = $true
      oldSessionsUiVerified = $true; firstSendVerified = $true; toolReadVerified = $true
      stopVerified = $true; restartVerified = $true; normalQuitVerified = $true
    }
    switch ($env:AUDIT_FAIL) {
      'retained-stale-audit' { $report.auditId = '0' * 32 }
      'retained-wrong-source' { $report.sourceSha = 'c' * 40 }
      'retained-wrong-executable' { $report.executableSha256 = 'd' * 64 }
      'retained-wrong-status' { $report.status = 'running' }
      'retained-not-ok' { $report.ok = $false }
      'retained-string-ok' { $report.ok = 'true' }
      'retained-numeric-ok' { $report.ok = 1 }
      'retained-proof-missing' { $report.Remove($env:AUDIT_PROOF) }
      'retained-proof-false' { $report[$env:AUDIT_PROOF] = $false }
      'retained-proof-string' { $report[$env:AUDIT_PROOF] = 'true' }
      'retained-proof-numeric' { $report[$env:AUDIT_PROOF] = 1 }
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'report.json')
    if ($env:AUDIT_FAIL -eq 'retained-exit') { $global:LASTEXITCODE = 1 }
    return
  }
  throw "Unexpected node command: $name"
}
function python {
  $values = @($args)
  $global:Calls.Add("profile-$($values[1])")
  $global:LASTEXITCODE = 0
  if ($values[1] -eq 'seed') {
    $profile = Get-ArgumentValue $values '--home'
    New-Item -ItemType Directory -Path $profile | Out-Null
    $userData = Split-Path $profile -Parent
    [IO.File]::WriteAllText((Join-Path $profile 'config.toml'), 'config_version = 1')
    [IO.File]::WriteAllText((Join-Path $userData 'desktop-credential.json'),
      '{"credential":"synthetic-no-access"}')
    if ($env:AUDIT_FAIL -eq 'marker-existing') {
      [IO.File]::WriteAllText((Join-Path $userData 'retained-interaction-audit.json'), '{}')
    }
  } elseif ($values[1] -eq 'verify-signed-retained') {
    if ($env:AUDIT_FAIL -eq 'preservation') { $global:LASTEXITCODE = 1 }
  } else { throw 'Unexpected profile operation.' }
}
try { $code = Invoke-SignedWindowsUpdateAudit @options }
catch { $code = 1; $failure = $_.Exception.Message }
@{ code = $code; calls = $global:Calls.ToArray(); failure = $failure
   preflightNodeCalls = $global:PreflightNodeCalls } |
  ConvertTo-Json -Depth 8 | Set-Content $env:AUDIT_OUTPUT
""",
        encoding="utf-8",
    )
    environment = {
        "AUDIT_SCRIPT": str(scripts / AUDIT.name),
        "AUDIT_INPUT": str(input_path),
        "AUDIT_OUTPUT": str(tmp_path / "harness-result.json"),
        "AUDIT_NATIVE_PROFILE": str(profile),
        "AUDIT_ROAMING_ROOT": str(roaming),
        "AUDIT_NODE_EXECUTABLE": node_executable,
        "RUNNER_TEMP": str(tmp_path),
    }
    return runner, environment, evidence


@pytest.mark.parametrize(
    ("failure", "last_call"),
    [
        ("", "profile-verify-signed-retained"),
        ("signature-1", "signature-1"),
        ("subscription", "observe-before-click"),
        ("handoff", "handoff"),
        ("attestation", "finish-attestation"),
        ("signature-2", "signature-2"),
        ("quit", "normal-quit"),
        ("first-send", "first-send-new-profile"),
        ("retained-exit", "retained-interaction"),
        ("preservation", "profile-verify-signed-retained"),
    ],
)
def test_signed_audit_orchestration_stays_incomplete_and_stops_on_failure(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str, last_call: str
) -> None:
    runner, environment, evidence = audit_harness
    run = _powershell(runner, **environment, AUDIT_FAIL=failure)
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == (1 if failure else 2), execution
    assert execution["calls"][-1] == last_call
    if failure == "subscription":
        assert "profile-seed" not in execution["calls"]
        assert "handoff" not in execution["calls"]
        assert not Path(environment["AUDIT_NATIVE_PROFILE"]).exists()
    result = json.loads((evidence / "result.json").read_text(encoding="utf-8-sig"))
    for unproven in (
        "ok",
        "releaseGatePassed",
        "automaticRestartVerified",
        "sessionRecoveryVerified",
    ):
        assert result[unproven] is False
    if not failure:
        assert result["stage"] == "postinstall-verified-with-gaps"
        assert result["firstSendScope"] == (
            "retained upgraded synthetic profile; loopback synthetic provider"
        )
        assert result["installedVersionVerified"] is True
        assert result["installedSignaturesVerified"] is True
        assert result["retainedSessionsVerified"] is True
        assert result["toolCallVerified"] is True
        assert result["stopAndRestartVerified"] is True
        assert result["credentialPreserved"] is True
        assert result["profilePreserved"] is True
        assert result["normalQuitObserved"] is True
        assert "retained-session-recovery" not in execution["calls"]
        assert "operator confirmed" in result["restartAttestation"]
        assert execution["calls"].index("observe-before-click") < execution["calls"].index(
            "handoff"
        )
        marker_path = Path(environment["AUDIT_NATIVE_PROFILE"]) / "retained-interaction-audit.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        report = json.loads(
            Path(result["retainedInteractionReport"]).read_text(encoding="utf-8-sig")
        )
        assert report["auditId"] == marker["auditId"]
        assert report["sourceSha"] == "b" * 40
        assert report["executableSha256"] == hashlib.sha256(b"synthetic installed B").hexdigest()
        assert report["markerSha256"] == hashlib.sha256(marker_path.read_bytes()).hexdigest()
        credential = Path(environment["AUDIT_NATIVE_PROFILE"]) / "desktop-credential.json"
        handoff = json.loads((evidence / "handoff.json").read_text(encoding="utf-8-sig"))
        assert report["credentialSha256"] == handoff["credentialSha256"]
        assert report["credentialSha256"] == hashlib.sha256(credential.read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["", "polling", "elevated"])
def test_standard_user_observation_does_not_require_elevating_the_client(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str
) -> None:
    runner, environment, evidence = audit_harness
    path = Path(environment["AUDIT_INPUT"])
    options = json.loads(path.read_text(encoding="utf-8"))
    options["ProcessObservationMode"] = "standard-user-polling"
    path.write_text(json.dumps(options), encoding="utf-8")
    run = _powershell(
        runner,
        **environment,
        AUDIT_FAIL=failure,
        AUDIT_ELEVATED="1" if failure == "elevated" else "0",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == (1 if failure else 2), execution
    assert "observe-before-click" not in execution["calls"]
    if failure:
        assert "profile-seed" not in execution["calls"]
        assert "handoff" not in execution["calls"]
        assert not Path(environment["AUDIT_NATIVE_PROFILE"]).exists()
    else:
        assert execution["calls"].index("poll-own-processes") < execution["calls"].index("handoff")
        result = json.loads((evidence / "result.json").read_text(encoding="utf-8-sig"))
        assert result["processObservationMode"] == "standard-user-polling"
        assert result["clientLauncherElevated"] is False
        assert result["restartObservation"]["Pid"] == 42
        assert result["normalQuitObserved"] is True
        assert result["automaticRestartVerified"] is False
        assert result["releaseGatePassed"] is False
        assert result["toolCallVerified"] is True
        assert result["stopAndRestartVerified"] is True
        assert result["retainedSessionsVerified"] is True
        assert result["sessionRecoveryVerified"] is False


def _run_audit_case(
    harness: tuple[Path, dict[str, str], Path],
    *,
    failure: str,
    mode: str = "cim-trace",
    proof: str = "",
    input_mode: str = "download",
) -> tuple[dict, dict]:
    runner, environment, evidence = harness
    path = Path(environment["AUDIT_INPUT"])
    options = json.loads(path.read_text(encoding="utf-8"))
    options["ProcessObservationMode"] = mode
    options["HandoffInputMode"] = input_mode
    path.write_text(json.dumps(options), encoding="utf-8")
    run = _powershell(runner, **environment, AUDIT_FAIL=failure, AUDIT_PROOF=proof)
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    result = json.loads((evidence / "result.json").read_text(encoding="utf-8-sig"))
    return execution, result


@pytest.mark.parametrize("timezone", ["Asia/Shanghai", "America/New_York", "UTC"])
@pytest.mark.parametrize("early", [False, True])
def test_json_handoff_time_preserves_restart_boundary(
    audit_harness: tuple[Path, dict[str, str], Path], timezone: str, early: bool
) -> None:
    # The full orchestration reads its handoff JSON. PowerShell 7 may produce a
    # UTC DateTime, while Windows PowerShell keeps the ISO string. Re-parsing a
    # typed value loses its timezone and can accept a process from before A quit.
    runner, environment, evidence = audit_harness
    execution, result = _run_audit_case(
        (runner, {**environment, "TZ": timezone}, evidence),
        failure="restart-before-handoff" if early else "",
        mode="standard-user-polling",
    )
    assert execution["code"] == (1 if early else 2), execution
    if early:
        assert "finish-attestation" not in execution["calls"]
        assert result["installedVersionVerified"] is False
        assert "new process after handoff" in result["error"]
    else:
        assert result["installedVersionVerified"] is True


@pytest.mark.parametrize(
    "failure",
    [
        "", "cache-mode", "cache-download-claim", "cache-publication-claim",
        "cache-numeric-download", "cache-marker", "cache-source", "cache-manifest",
        "cache-no-restart", "cache-string-restart", "cache-not-staged",
    ],
)
def test_cached_input_is_bound_and_cannot_claim_download_or_publication(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str
) -> None:
    execution, result = _run_audit_case(
        audit_harness, failure=failure, mode="standard-user-polling", input_mode="verified-cache"
    )
    assert execution["code"] == (1 if failure else 2), execution
    assert result["handoffInputMode"] == "verified-cache"
    assert result["downloadVerified"] is False
    assert result["remotePublicationVerified"] is False
    assert result["releaseGatePassed"] is False
    if failure:
        assert execution["calls"][-1] == "handoff"
        assert "finish-attestation" not in execution["calls"]
    else:
        assert result["cacheRestoreVerified"] is True
        assert result["cacheRestartVerified"] is True
        assert result["candidateValidation"] == "production-parser-on-local-fixture"
        assert result["profilePreserved"] is True
        assert "retained-interaction" in execution["calls"]
        assert any("does not verify remote publication" in gap for gap in result["gaps"])


@pytest.mark.parametrize("mode", ["cim-trace", "standard-user-polling"])
@pytest.mark.parametrize("failure", ["quit-cim-error", "quit-pid-reused", "quit-still-alive"])
def test_quit_requires_a_successful_cim_query_and_uses_creation_identity(
    audit_harness: tuple[Path, dict[str, str], Path], mode: str, failure: str
) -> None:
    execution, result = _run_audit_case(audit_harness, failure=failure, mode=mode)
    old_identity_exited = failure == "quit-pid-reused"
    assert execution["code"] == (2 if old_identity_exited else 1), execution
    assert failure in execution["calls"]
    assert result["normalQuitObserved"] is old_identity_exited
    assert ("retained-interaction" in execution["calls"]) is old_identity_exited
    assert result["ok"] is False
    assert result["releaseGatePassed"] is False
    if failure == "quit-cim-error":
        assert "Synthetic CIM query unavailable" in result["error"]
        assert "first-send-new-profile" not in execution["calls"]
    elif failure == "quit-still-alive":
        assert "did not exit after Quit" in result["error"]
        assert "first-send-new-profile" not in execution["calls"]
    else:
        assert result["quitProcessSnapshot"][0]["Pid"] == 42
        assert result["toolCallVerified"] is True


@pytest.mark.parametrize("mode", ["cim-trace", "standard-user-polling"])
def test_restart_pid_reuse_is_rejected_before_postinstall_quit(
    audit_harness: tuple[Path, dict[str, str], Path], mode: str
) -> None:
    execution, result = _run_audit_case(audit_harness, failure="postinstall-pid-reused", mode=mode)
    assert execution["code"] == 1
    assert "changed identity before postinstall verification" in result["error"]
    assert "normal-quit" not in execution["calls"]
    assert "retained-interaction" not in execution["calls"]
    assert result["normalQuitObserved"] is False


@pytest.mark.parametrize(
    "failure",
    [
        "retained-missing-report",
        "retained-stale-audit",
        "retained-wrong-source",
        "retained-wrong-executable",
        "retained-wrong-status",
        "retained-not-ok",
        "retained-string-ok",
        "retained-numeric-ok",
    ],
)
def test_retained_probe_requires_a_current_successful_bound_report(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str
) -> None:
    execution, result = _run_audit_case(audit_harness, failure=failure)
    assert execution["code"] == 1, execution
    assert execution["calls"][-1] == "retained-interaction"
    assert result["stage"] == "failed"
    assert result["toolCallVerified"] is False
    assert result["stopAndRestartVerified"] is False
    assert result.get("retainedSessionsVerified", False) is False
    assert result["ok"] is False
    assert result["releaseGatePassed"] is False


@pytest.mark.parametrize("failure", ["retained-proof-missing", "retained-proof-false"])
@pytest.mark.parametrize(
    "proof",
    [
        "credentialPreserved",
        "configPreserved",
        "oldSessionsVerified",
        "oldSessionsUiVerified",
        "firstSendVerified",
        "toolReadVerified",
        "stopVerified",
        "restartVerified",
        "normalQuitVerified",
    ],
)
def test_retained_probe_cannot_omit_any_required_proof(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str, proof: str
) -> None:
    execution, result = _run_audit_case(audit_harness, failure=failure, proof=proof)
    assert execution["code"] == 1, execution
    assert f"lacks proof: {proof}" in result["error"]
    assert execution["calls"][-1] == "retained-interaction"
    assert result["toolCallVerified"] is False
    assert result["stopAndRestartVerified"] is False
    assert result["profilePreserved"] is False
    assert result["releaseGatePassed"] is False


@pytest.mark.parametrize("failure", ["retained-proof-string", "retained-proof-numeric"])
def test_retained_probe_proofs_must_be_json_booleans(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str
) -> None:
    execution, result = _run_audit_case(audit_harness, failure=failure, proof="credentialPreserved")
    assert execution["code"] == 1, execution
    assert "lacks proof: credentialPreserved" in result["error"]
    assert result["toolCallVerified"] is False
    assert result["releaseGatePassed"] is False


@pytest.mark.parametrize(
    ("failure", "last_call"),
    [
        ("handoff-missing-credential", "handoff"),
        ("handoff-stale-credential", "first-send-new-profile"),
        ("credential-changed", "first-send-new-profile"),
        ("marker-existing", "first-send-new-profile"),
    ],
)
def test_retained_inputs_must_match_the_handoff_and_have_a_fresh_marker(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str, last_call: str
) -> None:
    execution, result = _run_audit_case(audit_harness, failure=failure)
    assert execution["code"] == 1, execution
    assert execution["calls"][-1] == last_call
    assert "retained-interaction" not in execution["calls"]
    assert result["toolCallVerified"] is False
    assert result["releaseGatePassed"] is False
    if failure in {"handoff-stale-credential", "credential-changed"}:
        assert "credential changed between A handoff" in result["error"]
    elif failure == "marker-existing":
        assert "ownership marker already exists" in result["error"]


@pytest.mark.parametrize(
    "invalid",
    ["existing-profile", "wrong-profile", "bad-hash", "bad-source", "legacy-A", "not-newer-B"],
)
def test_signed_audit_preflight_refuses_before_native_actions(
    audit_harness: tuple[Path, dict[str, str], Path], invalid: str
) -> None:
    runner, environment, evidence = audit_harness
    path = Path(environment["AUDIT_INPUT"])
    options = json.loads(path.read_text(encoding="utf-8"))
    if invalid == "existing-profile":
        Path(options["UserDataDir"]).mkdir()
    elif invalid == "wrong-profile":
        options["UserDataDir"] += "-redirected"
    elif invalid == "bad-hash":
        options["CandidateInstallerSha256"] = "0" * 64
    elif invalid == "bad-source":
        options["CandidateSourceSha"] = "main"
    elif invalid == "legacy-A":
        options["BaselineVersion"] = "0.5.4"
    else:
        options["BaselineVersion"] = "0.5.7"
    path.write_text(json.dumps(options), encoding="utf-8")
    run = _powershell(runner, **environment)
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == 1
    assert execution["calls"] == []
    assert not evidence.exists()


@pytest.mark.parametrize("existing_native", [True, False])
def test_display_name_alias_cannot_hide_or_replace_the_native_profile(
    audit_harness: tuple[Path, dict[str, str], Path], existing_native: bool,
) -> None:
    runner, environment, evidence = audit_harness
    roaming = Path(environment["AUDIT_ROAMING_ROOT"])
    native = Path(environment["AUDIT_NATIVE_PROFILE"])
    brand_alias = roaming / "OpenSquilla"
    sentinel = native / "existing-profile-sentinel.txt"
    if existing_native:
        native.mkdir()
        sentinel.write_bytes(b"preserve existing native profile")
    else:
        config_path = Path(environment["AUDIT_INPUT"])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["UserDataDir"] = str(brand_alias)
        config_path.write_text(json.dumps(config), encoding="utf-8")
    run = _powershell(runner, **environment)
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == 1, execution
    assert execution["calls"] == [], "No signature, seed, driver or installer may run"
    assert not evidence.exists()
    assert not brand_alias.exists()
    if existing_native:
        assert sentinel.read_bytes() == b"preserve existing native profile"
        assert list(native.iterdir()) == [sentinel]
    else:
        assert not native.exists()


@pytest.mark.parametrize("package_status", ["122", "0", "5", "15701", "unavailable"])
def test_packaged_or_unknown_launcher_fails_before_paths_plan_and_native_actions(
    audit_harness: tuple[Path, dict[str, str], Path], package_status: str
) -> None:
    runner, environment, evidence = audit_harness
    # Invalid plan data is intentional: launcher refusal must precede even plan
    # construction, as well as all signature, profile, and driver operations.
    config_path = Path(environment["AUDIT_INPUT"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["BaselineSourceSha"] = "invalid-plan-source"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    run = _powershell(runner, **environment, AUDIT_PACKAGE_STATUS=package_status)
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == 1, execution
    assert execution["calls"] == []
    assert execution["preflightNodeCalls"] == 0
    if package_status == "unavailable":
        assert "package API unavailable" in execution["failure"]
    else:
        assert f"GetCurrentPackageFullName returned {package_status}" in execution["failure"]
        assert "APPMODEL_ERROR_NO_PACKAGE (15700)" in execution["failure"]
    assert not evidence.exists()
    assert not Path(environment["AUDIT_NATIVE_PROFILE"]).exists()


def test_native_path_probe_failure_does_not_create_evidence_or_seed_profile(
    audit_harness: tuple[Path, dict[str, str], Path],
) -> None:
    runner, environment, evidence = audit_harness
    run = _powershell(runner, **environment, AUDIT_FAIL="node-preflight")
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == 1, execution
    assert "Native Roaming path preflight failed" in execution["failure"]
    assert execution["preflightNodeCalls"] == 1
    assert execution["calls"] == []
    assert not evidence.exists()
    assert not Path(environment["AUDIT_NATIVE_PROFILE"]).exists()


@pytest.mark.parametrize(
    "failure",
    [
        "write-view-redirected", "write-view-string", "write-view-retained",
        "write-view-no-cleanup", "write-view-incomplete-cleanup", "write-view-cleanup-string",
    ],
)
def test_write_view_must_be_native_and_cleaned_before_evidence_signature_or_seed(
    audit_harness: tuple[Path, dict[str, str], Path], failure: str
) -> None:
    runner, environment, evidence = audit_harness
    run = _powershell(runner, **environment, AUDIT_FAIL=failure)
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == 1, execution
    assert "write-view preflight failed before profile creation" in execution["failure"]
    assert execution["calls"] == ["write-view"]
    assert not evidence.exists()
    assert not Path(environment["AUDIT_NATIVE_PROFILE"]).exists()


def test_real_node_rejects_redirected_roaming_parent_before_seeding(
    audit_harness: tuple[Path, dict[str, str], Path], tmp_path: Path
) -> None:
    runner, environment, evidence = audit_harness
    parent = tmp_path / "synthetic-roaming"
    parent.mkdir()
    alias = tmp_path / "redirected-roaming"
    # A Windows junction needs no symlink privilege. Both paths belong only to
    # this newly created temporary fixture, not the account's actual Roaming.
    link = subprocess.run(
        [
            environment["AUDIT_NODE_EXECUTABLE"],
            "-e",
            "require('node:fs').symlinkSync(process.argv[1], process.argv[2], 'junction')",
            str(parent),
            str(alias),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    (parent / "@opensquilla").mkdir()
    native_profile = alias / "@opensquilla" / "desktop-electron"
    config_path = Path(environment["AUDIT_INPUT"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["UserDataDir"] = str(native_profile)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    run = _powershell(
        runner, **{
            **environment, "AUDIT_NATIVE_PROFILE": str(native_profile),
            "AUDIT_ROAMING_ROOT": str(alias),
        }
    )
    assert run.returncode == 0, run.stdout + run.stderr
    execution = json.loads(Path(environment["AUDIT_OUTPUT"]).read_text(encoding="utf-8-sig"))
    assert execution["code"] == 1, execution
    assert "Native Roaming parent is redirected" in run.stderr + execution["failure"]
    assert execution["preflightNodeCalls"] == 1
    assert execution["calls"] == []
    assert not evidence.exists()
    assert not native_profile.exists()
    assert list((parent / "@opensquilla").iterdir()) == []


def test_signed_restart_observation_rejects_old_pid_wrong_path_and_early_start(
    tmp_path: Path,
) -> None:
    runner = tmp_path / "observation.ps1"
    runner.write_text(
        r"""
. $env:AUDIT_SCRIPT
$when = [datetime]::UtcNow
$starts = @(
  [pscustomobject]@{ Pid=41; Path='target.exe'; StartedAt=$when.AddSeconds(1)
    CommandLine='target.exe --updated' },
  [pscustomobject]@{ Pid=42; Path='other.exe'; StartedAt=$when.AddSeconds(1)
    CommandLine='target.exe --updated' },
  [pscustomobject]@{ Pid=43; Path='target.exe'; StartedAt=$when.AddSeconds(-1)
    CommandLine='target.exe --updated' },
  [pscustomobject]@{ Pid=44; Path='target.exe'; StartedAt=$when.AddSeconds(1)
    CommandLine='target.exe --updated --type=renderer' },
  [pscustomobject]@{ Pid=45; Path='target.exe'; StartedAt=$when.AddSeconds(1)
    CommandLine='target.exe' },
  [pscustomobject]@{ Pid=46; Path='target.exe'; StartedAt=$when.AddSeconds(1)
    CommandLine='target.exe --updated "--type=gpu-process"' }
)
if (Find-SignedRestartCandidate $starts 'target.exe' $when 41) {
  throw 'Accepted unrelated process.'
}
$starts += [pscustomobject]@{ Pid=47; Path='target.exe'; StartedAt=$when
  CommandLine='"target.exe" "--updated"' }
if ((Find-SignedRestartCandidate $starts 'target.exe' $when 41).Pid -ne 47) {
  throw 'Did not select the main process.'
}
if (Test-SignedAuditVersion '0.5.7.1' '0.5.7') { throw 'Accepted stale PE variant.' }
if (-not (Test-SignedAuditVersion '0.5.7.0' '0.5.7')) { throw 'Rejected Windows PE normalization.' }
""",
        encoding="utf-8",
    )
    run = _powershell(runner, AUDIT_SCRIPT=str(AUDIT))
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize(
    "invalid", ["", "unknown-field", "missing-field", "manual-baseline-in-signed-mode"]
)
def test_existing_upgrade_entry_dispatches_signed_config_without_changing_manual_baselines(
    tmp_path: Path, invalid: str
) -> None:
    entry = tmp_path / "verify-release-windows-upgrade.ps1"
    entry.write_bytes((ROOT / ".github/scripts" / entry.name).read_bytes())
    (tmp_path / AUDIT.name).write_text(
        "@{ args = $args } | ConvertTo-Json -Depth 6 | Set-Content $env:AUDIT_DISPATCH\nexit 2\n",
        encoding="utf-8",
    )
    config = dict.fromkeys(
        (
            "InstallRoot",
            "UserDataDir",
            "EvidenceRoot",
            "BaselineVersion",
            "BaselineExecutableSha256",
            "BaselineSourceSha",
            "CandidateInstaller",
            "CandidateInstallerSha256",
            "CandidateSourceSha",
            "ChannelManifest",
        ),
        "synthetic-pinned-value",
    )
    if invalid == "unknown-field":
        config["SkipSignatureCheck"] = "true"
    elif invalid == "missing-field":
        del config["BaselineSourceSha"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    runner = tmp_path / "dispatch.ps1"
    runner.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "if ($env:AUDIT_INVALID -eq 'manual-baseline-in-signed-mode') {\n"
        "  & $env:AUDIT_ENTRY -SignedAuditConfigPath $env:AUDIT_CONFIG -BaselineVersion 0.5.6\n"
        "} else { & $env:AUDIT_ENTRY -SignedAuditConfigPath $env:AUDIT_CONFIG }\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    dispatched = tmp_path / "dispatched.json"
    result = _powershell(
        runner,
        AUDIT_ENTRY=str(entry),
        AUDIT_CONFIG=str(config_path),
        AUDIT_DISPATCH=str(dispatched),
        AUDIT_INVALID=invalid,
    )
    if invalid:
        assert result.returncode != 0
        assert not dispatched.exists()
    else:
        assert result.returncode == 2, result.stdout + result.stderr
        args = json.loads(dispatched.read_text(encoding="utf-8-sig"))["args"]
        assert "-BaselineSourceSha:" in args
        assert "-CandidateInstallerSha256:" in args


@pytest.fixture
def write_view_request(tmp_path: Path) -> dict:
    roaming = tmp_path.resolve() / "Roaming"
    roaming.mkdir()
    return {"auditId": str(uuid.uuid4()), "role": "python", "roamingParent": str(roaming)}


@pytest.mark.parametrize("role", ["node", "python"])
def test_native_marker_roundtrip_and_exact_empty_cleanup(
    write_view_request: dict, role: str
) -> None:
    write_view_request["role"] = role
    receipt = WRITE_VIEW.handle({**write_view_request, "action": "create"})["receipt"]
    assert receipt["native"] is True
    assert WRITE_VIEW.handle({**write_view_request, "action": "inspect"})["receipt"] == receipt
    removed = WRITE_VIEW.handle({**write_view_request, "action": "cleanup", "receipt": receipt})
    assert removed["removed"] == receipt["actual"]
    assert list(Path(write_view_request["roamingParent"]).iterdir()) == []


def test_creation_never_reuses_existing_directory(write_view_request: dict) -> None:
    _, requested, _ = WRITE_VIEW.validate(write_view_request)
    requested.mkdir()
    (requested / "preserve.txt").write_text("unrelated")
    with pytest.raises(ValueError, match="already exists"):
        WRITE_VIEW.handle({**write_view_request, "action": "create"})
    assert (requested / "preserve.txt").read_text() == "unrelated"


def test_known_virtualized_receipt_can_only_clean_its_owned_fixture(
    write_view_request: dict,
) -> None:
    roaming, requested, marker = WRITE_VIEW.validate(write_view_request)
    actual = roaming.parent / "Local/Packages/Fixture.Package/LocalCache/Roaming" / requested.name
    actual.mkdir(parents=True)
    (actual / "marker.txt").write_bytes(marker)
    receipt = WRITE_VIEW.snapshot(write_view_request, actual)
    assert receipt["native"] is False
    assert WRITE_VIEW.handle({**write_view_request, "action": "cleanup", "receipt": receipt}) == {
        "removed": str(actual)
    }
    assert not actual.exists()
    assert actual.parent.is_dir()


@pytest.mark.parametrize("change", ["nonce", "directory-identity", "marker-identity", "extra-file"])
def test_changed_probe_is_retained_without_any_deletion(
    write_view_request: dict, change: str
) -> None:
    receipt = WRITE_VIEW.handle({**write_view_request, "action": "create"})["receipt"]
    directory = Path(receipt["actual"])
    if change == "nonce":
        (directory / "marker.txt").write_text("not this probe")
    elif change == "directory-identity":
        receipt["directoryIdentity"][1] = "invalid"
    elif change == "marker-identity":
        receipt["markerIdentity"][1] = "invalid"
    else:
        (directory / "preserve.txt").write_text("unrelated")
    with pytest.raises(ValueError):
        WRITE_VIEW.handle({**write_view_request, "action": "cleanup", "receipt": receipt})
    assert directory.is_dir()
    assert (directory / "marker.txt").is_file()
    if change == "extra-file":
        assert (directory / "preserve.txt").read_text() == "unrelated"


def test_cleanup_rejects_a_receipt_outside_both_allowed_roots(
    write_view_request: dict, tmp_path: Path
) -> None:
    _, requested, marker = WRITE_VIEW.validate(write_view_request)
    outside = tmp_path / "not-an-approved-root" / requested.name
    outside.mkdir(parents=True)
    (outside / "marker.txt").write_bytes(marker)
    with pytest.raises(ValueError, match="Unexpected destination"):
        WRITE_VIEW.snapshot(write_view_request, outside)
    assert (outside / "marker.txt").read_bytes() == marker


def test_cleanup_rejects_other_uuid_and_role(write_view_request: dict) -> None:
    receipt = WRITE_VIEW.handle({**write_view_request, "action": "create"})["receipt"]
    with pytest.raises(ValueError, match="another probe"):
        WRITE_VIEW.handle({
            **write_view_request, "auditId": str(uuid.uuid4()),
            "action": "cleanup", "receipt": receipt,
        })
    assert Path(receipt["markerActual"]).is_file()


def test_reparse_or_symlink_directory_is_never_followed_for_cleanup(
    write_view_request: dict, tmp_path: Path
) -> None:
    node = shutil.which("node")
    assert node
    _, requested, marker = WRITE_VIEW.validate(write_view_request)
    target = tmp_path / "unrelated-target"
    target.mkdir()
    (target / "marker.txt").write_bytes(marker)
    subprocess.run(
        [node, "-e", "require('node:fs').symlinkSync(process.argv[1], process.argv[2], 'junction')",
         str(target), str(requested)],
        check=True, capture_output=True, timeout=10,
    )
    with pytest.raises(ValueError, match="Reparse/symlink"):
        WRITE_VIEW.snapshot(write_view_request, requested)
    assert (target / "marker.txt").read_bytes() == marker


def test_real_node_and_frozen_python_complete_only_in_new_temporary_parent(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node
    roaming = tmp_path.resolve() / "Roaming"
    roaming.mkdir()
    run = subprocess.run(
        [node, str(ROOT / ".github/scripts/verify-windows-native-write-view.mjs"),
         str(roaming), sys.executable],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["nativeWriteViewVerified"] is True
    assert len(result["observations"]) == len(result["cleanup"]) == 2
    assert all(entry["removed"] is True for entry in result["cleanup"])
    assert result["retainedPaths"] == []
    assert list(roaming.iterdir()) == []
    assert not (roaming / "OpenSquilla").exists()
