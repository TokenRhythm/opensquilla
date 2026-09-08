from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / ".github/scripts/verify-release-windows-signed-update.ps1"


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


@pytest.fixture
def audit_harness(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Run the real orchestration with OS/process boundaries replaced in a copy.

    No product bypass is added. No Electron, installer, signing, real profile,
    or process kill is run; the native gate needs separate operator evidence.
    """
    repo = tmp_path / "repo"
    scripts = repo / ".github/scripts"
    scripts.mkdir(parents=True)
    source = AUDIT.read_text(encoding="utf-8")
    replacements = {
        "if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT)": "if ($false)",
        "Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'OpenSquilla'": (
            "$env:AUDIT_NATIVE_PROFILE"
        ),
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
    profile = tmp_path / "native-profile"
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
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction)
  if ($Filter -eq 'ProcessId=42' -and -not $global:QuitObserved) {
    return [pscustomobject]@{
      ExecutablePath = $global:TargetExecutable; CreationDate = $global:CreatedAt
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
  if ($name -eq 'test-packaged-real-update-flow.mjs') {
    $global:Calls.Add('handoff')
    if ($env:AUDIT_FAIL -eq 'handoff') { $global:LASTEXITCODE = 1; return }
    $when = [datetime]::UtcNow.AddSeconds(-1)
    @{
      stage = 'installer-handoff'; handoffObserved = $true; requiresPostInstallVerification = $true
      ok = $false; fromVersion = $options.BaselineVersion; toVersion = '0.5.7'
      sha256 = $options.CandidateInstallerSha256; sourceSha = $options.CandidateSourceSha
      handoffStartedAt = $when.ToString('o'); oldPid = 41
    } | ConvertTo-Json | Set-Content (Get-ArgumentValue $values '--ready-output')
    $global:SyntheticVersion = '0.5.7'
    # A shell broker parent is valid observation, never machine causality proof.
    $global:Queue.Enqueue([pscustomobject]@{
      Pid = 42; ParentPid = 9; StartedAt = [datetime]::UtcNow; CreatedAt = $global:CreatedAt
      Path = $global:TargetExecutable; CommandLine = '"OpenSquilla.exe" "--updated"'
    })
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
    return
  }
  if ($name -eq 'test-packaged-session-recovery.mjs') {
    $global:Calls.Add('retained-session-recovery')
    if ((Get-ArgumentValue $values '--user-data-dir') -ne $options.UserDataDir -or
        (Get-ArgumentValue $values '--session-key') -ne
          'agent:main:webchat:release-recovery-long-session' -or
        (Get-ArgumentValue $values '--switch-session-key') -ne
          'agent:main:webchat:release-recovery-switch-session' -or
        (Get-ArgumentValue $values '--label') -ne 'signed-update-audit') {
      throw 'Retained probe arguments do not match seed.'
    }
    if ($env:AUDIT_FAIL -eq 'session') { $global:LASTEXITCODE = 1 }
    return
  }
  throw "Unexpected node command: $name"
}
function python {
  $values = @($args)
  $global:Calls.Add("profile-$($values[1])")
  $global:LASTEXITCODE = 0
  if ($values[1] -eq 'seed') {
    New-Item -ItemType Directory -Path (Get-ArgumentValue $values '--home') | Out-Null
  } elseif ($values[1] -eq 'verify-runtime') {
    if ($env:AUDIT_FAIL -eq 'preservation') { $global:LASTEXITCODE = 1 }
  } else { throw 'Unexpected profile operation.' }
}
try { $code = Invoke-SignedWindowsUpdateAudit @options }
catch { $code = 1; $failure = $_.Exception.Message }
@{ code = $code; calls = $global:Calls.ToArray(); failure = $failure } |
  ConvertTo-Json -Depth 8 | Set-Content $env:AUDIT_OUTPUT
""",
        encoding="utf-8",
    )
    environment = {
        "AUDIT_SCRIPT": str(scripts / AUDIT.name),
        "AUDIT_INPUT": str(input_path),
        "AUDIT_OUTPUT": str(tmp_path / "harness-result.json"),
        "AUDIT_NATIVE_PROFILE": str(profile),
        "RUNNER_TEMP": str(tmp_path),
    }
    return runner, environment, evidence


@pytest.mark.parametrize(
    ("failure", "last_call"),
    [
        ("", "profile-verify-runtime"),
        ("signature-1", "signature-1"),
        ("subscription", "observe-before-click"),
        ("handoff", "handoff"),
        ("attestation", "finish-attestation"),
        ("signature-2", "signature-2"),
        ("quit", "normal-quit"),
        ("first-send", "first-send-new-profile"),
        ("session", "retained-session-recovery"),
        ("preservation", "profile-verify-runtime"),
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
        "stopAndRestartVerified",
        "toolCallVerified",
    ):
        assert result[unproven] is False
    if not failure:
        assert result["stage"] == "postinstall-verified-with-gaps"
        assert result["firstSendScope"] == "new synthetic profile only"
        assert result["installedVersionVerified"] is True
        assert result["installedSignaturesVerified"] is True
        assert result["sessionRecoveryVerified"] is True
        assert result["profilePreserved"] is True
        assert result["normalQuitObserved"] is True
        assert "operator confirmed" in result["restartAttestation"]
        assert execution["calls"].index("observe-before-click") < execution["calls"].index(
            "handoff"
        )


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
