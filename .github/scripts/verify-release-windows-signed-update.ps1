[CmdletBinding()]
param(
  [string]$InstallRoot,
  [string]$UserDataDir,
  [string]$EvidenceRoot,
  [string]$BaselineVersion,
  [string]$BaselineExecutableSha256,
  [string]$BaselineSourceSha,
  [string]$CandidateInstaller,
  [string]$CandidateInstallerSha256,
  [string]$CandidateSourceSha,
  [string]$ChannelManifest,
  [ValidateSet('cim-trace', 'standard-user-polling')]
  [string]$ProcessObservationMode = 'cim-trace',
  [ValidateSet('download', 'verified-cache')]
  [string]$HandoffInputMode = 'download',
  [ValidateRange(30, 1800)][int]$InstallTimeoutSeconds = 600
)

$ErrorActionPreference = 'Stop'

function Test-SignedAuditVersion([string]$Actual, [string]$Expected) {
  return $Actual -ceq $Expected -or $Actual -ceq "$Expected.0"
}

function Get-SignedAuditLauncherPackageStatus {
  if (-not ('OpenSquillaSignedAudit.PackageIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace OpenSquillaSignedAudit {
  public static class PackageIdentity {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    public static extern int GetCurrentPackageFullName(ref uint length, IntPtr name);
  }
}
'@
  }
  [uint32]$length = 0
  return [OpenSquillaSignedAudit.PackageIdentity]::GetCurrentPackageFullName([ref]$length, [IntPtr]::Zero)
}

function Get-SignedAuditNodeExecutable {
  return (Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
}

function Get-SignedAuditPythonExecutable {
  return (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
}

function Get-SignedAuditRoamingRoot {
  return [Environment]::GetFolderPath('ApplicationData')
}

function Get-SignedAuditNativeUserData([string]$RoamingRoot) {
  if (-not $RoamingRoot -or -not [IO.Path]::IsPathRooted($RoamingRoot)) {
    throw 'The native Roaming root must be absolute.'
  }
  # Electron uses the packaged package.json name, not NSIS productName.
  # Keep this identity bound to desktop/electron/package.json by a contract test.
  return [IO.Path]::GetFullPath((Join-Path $RoamingRoot '@opensquilla/desktop-electron'))
}

function Invoke-SignedAuditWriteViewPreflight {
  param([string]$RoamingRoot, [string]$NodeExecutable, [string]$PythonExecutable)
  $roamingParent = $RoamingRoot
  $probe = Join-Path $PSScriptRoot 'verify-windows-native-write-view.mjs'
  $output = & $NodeExecutable $probe $roamingParent $PythonExecutable
  $exitCode = $LASTEXITCODE
  $result = ($output -join "`n") | ConvertFrom-Json
  if ($exitCode -ne 0 -or $result.nativeWriteViewVerified -isnot [bool] -or
      $result.nativeWriteViewVerified -ne $true -or @($result.cleanup).Count -ne 2 -or
      @($result.cleanup | Where-Object { $_.removed -isnot [bool] -or $_.removed -ne $true }).Count -ne 0 -or
      @($result.retainedPaths).Count -ne 0) {
    $details = $result | ConvertTo-Json -Depth 8 -Compress
    throw "Native Node/Python write-view preflight failed before profile creation. Use an ordinary desktop shell. Retained probe paths, if any, need review: $details"
  }
  return $result
}

function Assert-SignedAuditNativeLauncher {
  param([string]$NativeUserDataDir, [string]$NodeExecutable)
  $packageStatus = Get-SignedAuditLauncherPackageStatus
  # Only APPMODEL_ERROR_NO_PACKAGE admits an unpackaged launcher. In particular,
  # ERROR_INSUFFICIENT_BUFFER (122) means this process has a package identity.
  if ($packageStatus -ne 15700) {
    throw "Use an unpackaged ordinary desktop PowerShell shell, outside Codex/MSIX. GetCurrentPackageFullName returned $packageStatus; expected APPMODEL_ERROR_NO_PACKAGE (15700). No audit profile was created."
  }
  $probe = @'
const { lstatSync, realpathSync } = require('node:fs');
const { dirname, isAbsolute, resolve } = require('node:path');
const input = process.argv[1];
if (!input || !isAbsolute(input)) throw new Error('Native profile path must be absolute.');
const nativeUserDataDir = resolve(input);
const roamingParent = dirname(nativeUserDataDir);
const info = lstatSync(roamingParent);
const canonicalRoamingParent = realpathSync(roamingParent);
const samePath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
if (!info.isDirectory() || info.isSymbolicLink() || !samePath(canonicalRoamingParent, roamingParent)) {
  throw new Error('Native Roaming parent is redirected. Use an unpackaged ordinary desktop shell; no audit profile was created.');
}
try {
  lstatSync(nativeUserDataDir);
  throw new Error('The native OpenSquilla profile already exists; use a fresh disposable account.');
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
process.stdout.write(JSON.stringify({ nativeUserDataDir, roamingParent, canonicalRoamingParent, profileAbsent: true }));
'@
  $output = & $NodeExecutable -e $probe $NativeUserDataDir
  if ($LASTEXITCODE -ne 0) {
    throw "Native Roaming path preflight failed in the audit Node executable (exit $LASTEXITCODE). Use an unpackaged ordinary desktop shell; no audit profile was created."
  }
  $paths = ($output -join "`n") | ConvertFrom-Json
  return [ordered]@{ packageIdentityStatus = $packageStatus; nodeExecutable = $NodeExecutable; paths = $paths }
}

function Get-SignedAuditPlan {
  param(
    [string]$InstallRoot, [string]$UserDataDir, [string]$NativeUserDataDir,
    [string]$EvidenceRoot, [string]$TemporaryRoot, [string]$BaselineVersion,
    [string]$BaselineExecutableSha256, [string]$BaselineSourceSha,
    [string]$CandidateInstaller, [string]$CandidateInstallerSha256,
    [string]$CandidateSourceSha, [string]$ChannelManifest
  )
  foreach ($value in @($InstallRoot, $UserDataDir, $NativeUserDataDir, $EvidenceRoot,
      $TemporaryRoot, $CandidateInstaller, $ChannelManifest)) {
    if (-not $value -or -not [IO.Path]::IsPathRooted($value)) {
      throw 'Every audit path must be explicit and absolute.'
    }
  }
  foreach ($sha in @($BaselineSourceSha, $CandidateSourceSha)) {
    if ($sha -cnotmatch '^[0-9a-f]{40}$') { throw 'Both build source SHAs must be full lowercase commit hashes.' }
  }
  foreach ($sha in @($BaselineExecutableSha256, $CandidateInstallerSha256)) {
    if ($sha -cnotmatch '^[0-9a-f]{64}$') { throw 'Both artifact SHA256 hashes must be pinned before the audit.' }
  }
  if ($BaselineVersion -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
      $BaselineVersion -in @('0.5.3', '0.5.4')) {
    throw 'Use a stable signed baseline with the new handoff capability; 0.5.3/0.5.4 retain the manual audit.'
  }
  $name = [IO.Path]::GetFileName($CandidateInstaller)
  if ($name -cnotmatch '^OpenSquilla-(?<version>(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))-win-x64\.exe$') {
    throw 'Candidate must have the canonical stable Windows installer filename.'
  }
  $candidateVersion = $Matches['version']
  if ([version]$candidateVersion -le [version]$BaselineVersion) { throw 'Candidate B must be newer than baseline A.' }
  $userData = [IO.Path]::GetFullPath($UserDataDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $nativeUserData = [IO.Path]::GetFullPath($NativeUserDataDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
  if (-not $userData.Equals($nativeUserData, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Use the disposable account native AppData/Roaming/@opensquilla/desktop-electron directory; NSIS does not inherit --user-data-dir.'
  }
  if (Test-Path -LiteralPath $userData) { throw 'The native OpenSquilla profile already exists; use a fresh disposable account.' }
  $evidence = [IO.Path]::GetFullPath($EvidenceRoot)
  $temporary = [IO.Path]::GetFullPath($TemporaryRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
  if (-not $evidence.StartsWith($temporary, [StringComparison]::OrdinalIgnoreCase) -or
      (Test-Path -LiteralPath $evidence)) {
    throw 'EvidenceRoot must be a new directory strictly inside the temporary directory.'
  }
  $install = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  foreach ($other in @($userData, $evidence)) {
    if ($other.Equals($install, [StringComparison]::OrdinalIgnoreCase) -or
        $other.StartsWith($install + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $install.StartsWith($other + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
      throw 'Installation, evidence, and synthetic profile directories must be separate.'
    }
  }
  return [pscustomobject]@{
    InstallRoot = $install; Executable = (Join-Path $install 'OpenSquilla.exe')
    UserDataDir = $userData; Profile = (Join-Path $userData 'opensquilla')
    EvidenceRoot = $evidence; CandidateVersion = $candidateVersion
    CandidateInstaller = [IO.Path]::GetFullPath($CandidateInstaller)
    ChannelManifest = [IO.Path]::GetFullPath($ChannelManifest)
  }
}

function Find-SignedRestartCandidate {
  param([object[]]$Starts, [string]$ExecutablePath, [datetime]$NotBefore, [int]$OldPid)
  # NSIS uses ExecShellAsUser, which can launch through the user's shell broker.
  # A new process is an observation; its cause needs separate Finish attestation.
  return @($Starts | Where-Object {
    $_.Path -ieq $ExecutablePath -and $_.StartedAt -ge $NotBefore -and $_.Pid -ne $OldPid -and
    $_.CommandLine -match '(?:^|\s)"?--updated"?(?=\s|$)' -and
    $_.CommandLine -notmatch '(?:^|\s)"?--type(?:=|\s|"|$)'
  } | Sort-Object StartedAt | Select-Object -Last 1) | Select-Object -First 1
}

function Test-SignedAuditElevated {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  return ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-SignedPollingStarts([string]$ExecutablePath) {
  # Ordinary users can inspect their own long-lived Finish-page launch without
  # elevating A just to subscribe to Win32_ProcessStartTrace. This is a snapshot,
  # not a complete event trace or proof that NSIS caused the launch.
  foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name='OpenSquilla.exe'")) {
    if ($process.ExecutablePath -ieq $ExecutablePath -and $process.CreationDate -and
        $process.CommandLine) {
      [pscustomobject]@{
        Pid = [int]$process.ProcessId; ParentPid = [int]$process.ParentProcessId
        StartedAt = $process.CreationDate.ToUniversalTime(); CreatedAt = $process.CreationDate
        Path = [string]$process.ExecutablePath; CommandLine = [string]$process.CommandLine
      }
    }
  }
}

function New-SignedAuditResult {
  param([string]$BaselineVersion, [string]$CandidateVersion, [string]$BaselineSourceSha,
    [string]$CandidateSourceSha, [string]$BaselineExecutableSha256, [string]$CandidateInstallerSha256)
  return [ordered]@{
    schemaVersion = 1; ok = $false; stage = 'preflight'; releaseGatePassed = $false
    fromVersion = $BaselineVersion; toVersion = $CandidateVersion
    provenance = [ordered]@{
      baselineSourceSha = $BaselineSourceSha; candidateSourceSha = $CandidateSourceSha
      baselineExecutableSha256 = $BaselineExecutableSha256; candidateInstallerSha256 = $CandidateInstallerSha256
      sourceBinding = 'operator-supplied build provenance; artifact hashes verified locally'
    }
    handoffObserved = $false; automaticRestartVerified = $false
    installedVersionVerified = $false; installedSignaturesVerified = $false
    firstSendVerified = $false; firstSendScope = 'new synthetic profile only'; sessionRecoveryVerified = $false
    normalQuitObserved = $false; stopAndRestartVerified = $false; profilePreserved = $false; toolCallVerified = $false
    gaps = @('Shell-brokered restart causality is operator-attested, not machine-proven.',
      'Retained-profile interaction requires the independently bound packaged probe.',
      'NSIS interruption after the old uninstaller starts has no verified rollback guarantee.',
      'Uninstall preservation is covered separately by the existing installer audit.')
  }
}

function Invoke-SignedWindowsUpdateAudit {
  param(
    [string]$InstallRoot, [string]$UserDataDir, [string]$EvidenceRoot,
    [string]$BaselineVersion, [string]$BaselineExecutableSha256, [string]$BaselineSourceSha,
    [string]$CandidateInstaller, [string]$CandidateInstallerSha256, [string]$CandidateSourceSha,
    [string]$ChannelManifest, [int]$InstallTimeoutSeconds = 600,
    [ValidateSet('cim-trace', 'standard-user-polling')]
    [string]$ProcessObservationMode = 'cim-trace',
    [ValidateSet('download', 'verified-cache')]
    [string]$HandoffInputMode = 'download'
  )
  if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'This audit requires Windows.' }
  $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
  $roamingRoot = Get-SignedAuditRoamingRoot
  $nativeUserData = Get-SignedAuditNativeUserData $roamingRoot
  # Check package identity and the same Node runtime's actual filesystem view
  # before plan creation, evidence writes, signature cache warming, or seeding.
  $nodeExecutable = Get-SignedAuditNodeExecutable
  $pythonExecutable = Get-SignedAuditPythonExecutable
  $launcherPreflight = Assert-SignedAuditNativeLauncher $nativeUserData $nodeExecutable
  $temporary = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
  $planArguments = @{} + $PSBoundParameters
  $null = $planArguments.Remove('InstallTimeoutSeconds')
  $null = $planArguments.Remove('ProcessObservationMode')
  $null = $planArguments.Remove('HandoffInputMode')
  $planArguments.NativeUserDataDir = $nativeUserData
  $planArguments.TemporaryRoot = $temporary
  $plan = Get-SignedAuditPlan @planArguments
  $launcherElevated = Test-SignedAuditElevated
  if ($ProcessObservationMode -eq 'standard-user-polling' -and $launcherElevated) {
    throw 'Standard-user polling must launch A from a non-elevated shell to preserve the UAC test boundary.'
  }
  foreach ($path in @($plan.Executable, $plan.CandidateInstaller, $plan.ChannelManifest)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required input is missing: $path" }
  }
  $existing = @(Get-CimInstance Win32_Process -Filter "Name='OpenSquilla.exe' OR Name='opensquilla-gateway.exe'" | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($plan.InstallRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
  })
  if ($existing.Count) { throw 'The selected baseline is already running; use a stopped disposable installation.' }
  $actualA = ([Diagnostics.FileVersionInfo]::GetVersionInfo($plan.Executable)).ProductVersion
  if (-not (Test-SignedAuditVersion $actualA $BaselineVersion)) { throw 'Installed A has a different PE version.' }
  if ((Get-FileHash -LiteralPath $plan.Executable -Algorithm SHA256).Hash.ToLowerInvariant() -cne $BaselineExecutableSha256 -or
      (Get-FileHash -LiteralPath $plan.CandidateInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -cne $CandidateInstallerSha256) {
    throw 'A or B does not match its pinned artifact hash.'
  }
  $manifest = Get-Content -LiteralPath $plan.ChannelManifest -Raw | ConvertFrom-Json
  if ($manifest.schemaVersion -ne 1 -or $manifest.version -cne $plan.CandidateVersion -or
      $manifest.tag -cne "v$($plan.CandidateVersion)" -or $manifest.prerelease -ne $false) {
    throw 'The controlled manifest does not identify candidate B.'
  }
  # Real writes to new UUID siblings are necessary: MSIX virtualization can
  # redirect new children even when package identity and parent realpath pass.
  # The probe never creates OpenSquilla and removes only its own verified
  # marker files and empty directories; uncertain cleanup fails and keeps them.
  $writeViewPreflight = Invoke-SignedAuditWriteViewPreflight $roamingRoot $nodeExecutable $pythonExecutable
  New-Item -ItemType Directory -Path $plan.EvidenceRoot | Out-Null
  $signatureCheck = Join-Path $repo '.github/scripts/verify-windows-signatures.ps1'
  $probe = Join-Path $repo '.github/scripts/verify-release-profile-preservation.py'
  $resultPath = Join-Path $plan.EvidenceRoot 'result.json'
  $result = New-SignedAuditResult $BaselineVersion $plan.CandidateVersion $BaselineSourceSha `
    $CandidateSourceSha $BaselineExecutableSha256 $CandidateInstallerSha256
  $result.provenance.channelManifestSha256 = (Get-FileHash -LiteralPath $plan.ChannelManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  $result.processObservationMode = $ProcessObservationMode
  $result.clientLauncherElevated = $launcherElevated
  $result.launcherPreflight = $launcherPreflight
  $result.writeViewPreflight = $writeViewPreflight
  $result.pythonExecutable = $pythonExecutable
  $result.handoffInputMode = $HandoffInputMode
  $result.downloadVerified = $false
  $result.remotePublicationVerified = $false
  $sourceId = 'OpenSquilla.SignedUpdate.' + [guid]::NewGuid().ToString('N')
  $subscription = $null
  $automaticPid = $null
  $originalOffline = $env:OPENSQUILLA_RECOVERY_OFFLINE
  $originalUpdate = $env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE
  try {
    & $signatureCheck -InstallerPath $plan.CandidateInstaller -InstalledRoot $plan.InstallRoot |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'preflight-signatures.txt')
    if ($LASTEXITCODE -ne 0) { throw 'A/B signature verification failed.' }
    # A must inherit the same native profile B will use from the NSIS Finish page.
    # No APPDATA/LOCALAPPDATA redirection or profile reuse is permitted.
    $env:OPENSQUILLA_RECOVERY_OFFLINE = '1'
    $env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE = '1'
    # Trace registration can require elevation. Establish it before creating the
    # fresh native profile, so an access denial cannot consume that precondition.
    $queue = [Collections.Concurrent.ConcurrentQueue[object]]::new()
    $observer = @{ Queue = $queue; Executable = $plan.Executable }
    if ($ProcessObservationMode -eq 'standard-user-polling') {
      # Probe CIM access before consuming the fresh-profile precondition.
      $null = @(Get-SignedPollingStarts $plan.Executable)
    } else {
      $subscription = Register-CimIndicationEvent -Query 'SELECT * FROM Win32_ProcessStartTrace' `
      -SourceIdentifier $sourceId -MessageData $observer -Action {
        $trace = $Event.SourceEventArgs.NewEvent
        if ($trace.ProcessName -ine 'OpenSquilla.exe') { return }
        $started = [datetime]::FromFileTimeUtc([long]$trace.TIME_CREATED)
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($trace.ProcessID)" -ErrorAction SilentlyContinue
        if ($process -and $process.ExecutablePath -ieq $Event.MessageData.Executable -and
            $process.CreationDate.ToUniversalTime() -le $started -and $Event.MessageData.Queue.Count -lt 1024) {
          $Event.MessageData.Queue.Enqueue([pscustomobject]@{
            Pid = [int]$trace.ProcessID; ParentPid = [int]$trace.ParentProcessID
            StartedAt = $started; CreatedAt = $process.CreationDate
            Path = [string]$process.ExecutablePath; CommandLine = [string]$process.CommandLine
          })
        }
      }
    }
    & $pythonExecutable $probe seed-signed-retained --home $plan.Profile --label signed-update-audit --external-root (Join-Path $plan.EvidenceRoot 'external-sentinels') |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'profile-seed.log')
    if ($LASTEXITCODE -ne 0) { throw 'Could not seed the isolated synthetic profile.' }
    $handoffMode = 'signed-handoff'
    $cachedArguments = @()
    if ($HandoffInputMode -eq 'verified-cache') {
      $handoffMode = 'signed-cached-handoff'
      $cacheMarkerPath = Join-Path $plan.UserDataDir 'cached-handoff-audit.json'
      if (Test-Path -LiteralPath $cacheMarkerPath) { throw 'The cached-handoff ownership marker already exists.' }
      $cacheMarker = [ordered]@{
        schemaVersion = 1; purpose = 'opensquilla-synthetic-cached-handoff-audit'
        auditId = [guid]::NewGuid().ToString('N'); seedLabel = 'signed-update-audit'
        userDataDir = $plan.UserDataDir; baselineVersion = $BaselineVersion
        expectedVersion = $plan.CandidateVersion; expectedSha256 = $CandidateInstallerSha256
        sourceSha = $CandidateSourceSha; baselineSourceSha = $BaselineSourceSha
        configSha256 = (Get-FileHash -LiteralPath (Join-Path $plan.Profile 'config.toml') -Algorithm SHA256).Hash.ToLowerInvariant()
      }
      $cacheMarker | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $cacheMarkerPath -Encoding utf8
      $cachedArguments = @('--cached-installer', $plan.CandidateInstaller, '--baseline-source-sha', $BaselineSourceSha)
    }
    $result.stage = 'waiting-for-installer-handoff'
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    $handoffPath = Join-Path $plan.EvidenceRoot 'handoff.json'
    & $nodeExecutable (Join-Path $repo 'desktop/electron/scripts/test-packaged-real-update-flow.mjs') `
      --mode $handoffMode --executable $plan.Executable --user-data-dir $plan.UserDataDir `
      --baseline-version $BaselineVersion --expected-version $plan.CandidateVersion `
      --channel-manifest $plan.ChannelManifest --expected-sha256 $CandidateInstallerSha256 `
      --source-sha $CandidateSourceSha --ready-output $handoffPath @cachedArguments 2>&1 |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'handoff-driver.log')
    if ($LASTEXITCODE -ne 0) { throw 'The packaged client did not complete a verified installer handoff.' }
    $handoff = Get-Content -LiteralPath $handoffPath -Raw | ConvertFrom-Json
    if ($handoff.credentialSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $handoff.stage -ne 'installer-handoff' -or $handoff.handoffObserved -ne $true -or
        $handoff.requiresPostInstallVerification -ne $true -or $handoff.ok -ne $false -or
        $handoff.mode -cne $handoffMode -or
        $handoff.fromVersion -cne $BaselineVersion -or $handoff.toVersion -cne $plan.CandidateVersion -or
        $handoff.sha256 -cne $CandidateInstallerSha256 -or $handoff.sourceSha -cne $CandidateSourceSha) {
      throw 'The handoff result does not match the pinned A-to-B audit.'
    }
    if ($HandoffInputMode -eq 'verified-cache') {
      $markerSha = (Get-FileHash -LiteralPath $cacheMarkerPath -Algorithm SHA256).Hash.ToLowerInvariant()
      if ($handoff.mode -cne 'signed-cached-handoff' -or
          $handoff.inputMode -cne 'verified-cache' -or
          $handoff.fixtureSource -cne 'local Actions artifact and local channel fixture' -or
          $handoff.candidateValidation -cne 'production-parser-on-local-fixture' -or
          $handoff.baselineSourceSha -cne $BaselineSourceSha -or
          $handoff.auditId -cne $cacheMarker.auditId -or $handoff.markerSha256 -cne $markerSha -or
          $handoff.installerSha256 -cne $CandidateInstallerSha256 -or
          $handoff.manifestSha256 -cne $result.provenance.channelManifestSha256) {
        throw 'Cached handoff provenance does not match this audit and its local fixture.'
      }
      foreach ($proof in @('cacheStagedAndVerified', 'cacheRestoreVerified', 'cacheRestartVerified')) {
        if ($handoff.$proof -isnot [bool] -or $handoff.$proof -ne $true) {
          throw "The cached handoff lacks actual proof: $proof"
        }
      }
      foreach ($excluded in @('downloadVerified', 'remotePublicationVerified')) {
        if ($handoff.$excluded -isnot [bool] -or $handoff.$excluded -ne $false) {
          throw "The cached handoff must not claim $excluded."
        }
      }
      $result.cacheRestoreVerified = $true
      $result.cacheRestartVerified = $true
      $result.candidateValidation = $handoff.candidateValidation
      $result.gaps += 'This cached-input cell does not verify remote publication, installer download, checksum fetch, or source fallback.'
    } else {
      if ($handoff.inputMode -cne 'download' -or $handoff.downloadVerified -isnot [bool] -or
          $handoff.downloadVerified -ne $true -or $handoff.remotePublicationVerified -isnot [bool] -or
          $handoff.remotePublicationVerified -ne $false) {
        throw 'The download audit requires its real download proof and cannot certify remote publication.'
      }
      $result.downloadVerified = $true
    }
    $result.handoffObserved = $true
    $result.stage = 'waiting-for-operator-installer-and-automatic-restart'
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    Write-Host 'Complete the NSIS wizard and leave Run OpenSquilla selected. Do not launch B manually.'
    # PowerShell 7 can deserialize ISO JSON timestamps as DateTime. Parsing
    # that object again stringifies it without its UTC kind and shifts the
    # handoff boundary in non-UTC zones. Cast preserves both typed and string
    # timestamps before normalizing to UTC.
    $notBefore = ([datetime]$handoff.handoffStartedAt).ToUniversalTime()
    $starts = [Collections.Generic.List[object]]::new()
    $deadline = [datetime]::UtcNow.AddSeconds($InstallTimeoutSeconds)
    $restart = $null
    while ([datetime]::UtcNow -lt $deadline) {
      if ($ProcessObservationMode -eq 'standard-user-polling') {
        $starts.Clear()
        foreach ($observed in @(Get-SignedPollingStarts $plan.Executable)) {
          $starts.Add($observed)
        }
      }
      $observed = $null
      while ($queue.TryDequeue([ref]$observed)) {
        if ($starts.Count -ge 1024) { throw 'Too many process starts; restart evidence is ambiguous.' }
        $starts.Add($observed)
      }
      if (Test-Path -LiteralPath $plan.Executable -PathType Leaf) {
        $actualB = ([Diagnostics.FileVersionInfo]::GetVersionInfo($plan.Executable)).ProductVersion
        if (Test-SignedAuditVersion $actualB $plan.CandidateVersion) {
          $restart = Find-SignedRestartCandidate -Starts $starts.ToArray() `
            -ExecutablePath $plan.Executable -NotBefore $notBefore -OldPid $handoff.oldPid
          if ($restart) { break }
        }
      }
      Start-Sleep -Milliseconds 250
    }
    if (-not $restart) { throw 'B installation and a new process after handoff were not both observed.' }
    $automaticPid = $restart.Pid
    $result.restartObservation = $restart
    Write-Host 'If B started from NSIS Finish with Run OpenSquilla selected and you did not launch it manually, type FINISH-AUTOLAUNCH'
    $attestation = Read-Host 'Finish/Run observation'
    if ($attestation -cne 'FINISH-AUTOLAUNCH') { throw 'Finish-page restart was not confirmed; a manual launch does not count.' }
    $result.restartAttestation = 'operator confirmed NSIS Finish/Run; shell-broker causality not machine-proven'
    $result.installedVersionVerified = $true
    & $signatureCheck -InstallerPath $plan.CandidateInstaller -InstalledRoot $plan.InstallRoot |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'postinstall-signatures.txt')
    if ($LASTEXITCODE -ne 0) { throw 'B installed executable signature verification failed.' }
    $result.installedSignaturesVerified = $true
    $result.stage = 'postinstall-probes'
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    # Capture B and its known descendants before the operator uses ordinary Quit.
    # Never force-kill a process that may still be persisting the retained profile.
    $live = Get-CimInstance Win32_Process -Filter "ProcessId=$automaticPid" -ErrorAction SilentlyContinue
    if (-not $live -or $live.ExecutablePath -ine $plan.Executable -or $live.CreationDate -ne $restart.CreatedAt) {
      throw 'Observed B main process exited or changed identity before postinstall verification.'
    }
    $snapshot = @(Get-CimInstance Win32_Process)
    $owned = [Collections.Generic.List[object]]::new()
    $owned.Add([pscustomobject]@{ Pid = $automaticPid; CreatedAt = $live.CreationDate })
    do {
      $added = $false
      foreach ($process in $snapshot) {
        if ($process.ParentProcessId -in @($owned.Pid) -and $process.ProcessId -notin @($owned.Pid)) {
          $owned.Add([pscustomobject]@{ Pid = $process.ProcessId; CreatedAt = $process.CreationDate })
          $added = $true
        }
      }
    } while ($added)
    $result.quitProcessSnapshot = $owned.ToArray()
    Write-Host 'Use the running B tray Quit command, then type QUIT (do not end it in Task Manager)'
    $quit = Read-Host 'Normal Quit observation'
    if ($quit -cne 'QUIT') { throw 'Normal Quit was not confirmed; processes and profile are retained for diagnosis.' }
    $deadline = [datetime]::UtcNow.AddSeconds([Math]::Min(90, $InstallTimeoutSeconds))
    do {
      $remaining = @($owned | Where-Object {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Pid)" -ErrorAction Stop
        $process -and $process.CreationDate -eq $_.CreatedAt
      })
      if (-not $remaining.Count) { break }
      Start-Sleep -Milliseconds 250
    } while ([datetime]::UtcNow -lt $deadline)
    if ($remaining.Count) { throw 'B or its captured child processes did not exit after Quit; no force cleanup was performed.' }
    $automaticPid = $null
    $result.normalQuitObserved = $true
    $result.quitScope = 'operator tray Quit attestation plus exit of B and captured descendants; later children are not proven'
    & $nodeExecutable (Join-Path $repo 'desktop/electron/scripts/test-packaged-first-send-renderer.mjs') `
      --executable $plan.Executable --user-data-dir (Join-Path $plan.EvidenceRoot 'first-send-new-profile') --iterations 1 2>&1 |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'first-send.log')
    if ($LASTEXITCODE -ne 0) { throw 'Installed B first-send/owned Gateway probe failed.' }
    $result.firstSendVerified = $true
    $credentialPath = Join-Path $plan.UserDataDir 'desktop-credential.json'
    $credentialSha = (Get-FileHash -LiteralPath $credentialPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($credentialSha -cne $handoff.credentialSha256) {
      throw 'The retained desktop credential changed between A handoff and the B interaction probe.'
    }
    $interaction = [ordered]@{
      schemaVersion = 1; purpose = 'opensquilla-synthetic-signed-update-audit'
      auditId = [guid]::NewGuid().ToString('N'); seedLabel = 'signed-update-audit'
      userDataDir = $plan.UserDataDir; executablePath = $plan.Executable
      expectedVersion = $plan.CandidateVersion; sourceSha = $CandidateSourceSha
      executableSha256 = (Get-FileHash -LiteralPath $plan.Executable -Algorithm SHA256).Hash.ToLowerInvariant()
      credentialSha256 = $credentialSha
      configSha256 = (Get-FileHash -LiteralPath (Join-Path $plan.Profile 'config.toml') -Algorithm SHA256).Hash.ToLowerInvariant()
      externalSentinelsDir = Join-Path $plan.EvidenceRoot 'external-sentinels'
    }
    $interactionManifest = Join-Path $plan.UserDataDir 'retained-interaction-audit.json'
    if (Test-Path -LiteralPath $interactionManifest) { throw 'The retained-interaction ownership marker already exists.' }
    $interaction | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $interactionManifest -Encoding utf8
    $interactionOutput = Join-Path $plan.EvidenceRoot 'retained-interaction'
    & $nodeExecutable (Join-Path $repo 'desktop/electron/scripts/test-packaged-retained-interaction.mjs') `
      --audit-manifest $interactionManifest --output-dir $interactionOutput 2>&1 |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'retained-interaction.log')
    if ($LASTEXITCODE -ne 0) { throw 'Installed B retained-profile interaction probe failed.' }
    $interactionResult = Get-Content -LiteralPath (Join-Path $interactionOutput 'report.json') -Raw | ConvertFrom-Json
    if ($interactionResult.ok -isnot [bool] -or $interactionResult.ok -ne $true -or
        $interactionResult.status -cne 'passed' -or
        $interactionResult.auditId -cne $interaction.auditId -or
        $interactionResult.sourceSha -cne $interaction.sourceSha -or
        $interactionResult.executableSha256 -cne $interaction.executableSha256) {
      throw 'The retained-profile report does not match this installed B audit.'
    }
    foreach ($proof in @('credentialPreserved', 'configPreserved', 'oldSessionsVerified',
        'oldSessionsUiVerified', 'firstSendVerified', 'toolReadVerified', 'stopVerified',
        'restartVerified', 'normalQuitVerified')) {
      if ($interactionResult.$proof -isnot [bool] -or $interactionResult.$proof -ne $true) {
        throw "The retained-profile report lacks proof: $proof"
      }
    }
    $result.firstSendScope = 'retained upgraded synthetic profile; loopback synthetic provider'
    $result.retainedSessionsVerified = $true
    $result.toolCallVerified = $true
    $result.stopAndRestartVerified = $true
    $result.credentialPreserved = $true
    $result.retainedInteractionReport = Join-Path $interactionOutput 'report.json'
    $result.gaps = @($result.gaps | Where-Object { $_ -ne 'Retained-profile interaction requires the independently bound packaged probe.' })
    & $pythonExecutable $probe verify-signed-retained --home $plan.Profile --label signed-update-audit `
      --external-root (Join-Path $plan.EvidenceRoot 'external-sentinels') |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'profile-preservation.log')
    if ($LASTEXITCODE -ne 0) { throw 'Postinstall probes changed retained profile data.' }
    $result.profilePreserved = $true
    $result.stage = 'postinstall-verified-with-gaps'
    # One upgrade cell cannot certify the Windows 10/11, scope, cancellation,
    # signature rejection, or network matrix. A separate aggregate gate is required.
    return 2
  } catch {
    $result.error = $_.Exception.Message
    $result.stage = 'failed'
    Write-Error -ErrorAction Continue $_
    return 1
  } finally {
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    if ($subscription) {
      Unregister-Event -SourceIdentifier $sourceId -ErrorAction SilentlyContinue
      Get-Event -SourceIdentifier $sourceId -ErrorAction SilentlyContinue | Remove-Event
      Remove-Job -Job $subscription -Force -ErrorAction SilentlyContinue
    }
    $env:OPENSQUILLA_RECOVERY_OFFLINE = $originalOffline
    $env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE = $originalUpdate
    Write-Host "Audit evidence retained at $($plan.EvidenceRoot). Release gate remains closed."
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  exit (Invoke-SignedWindowsUpdateAudit @PSBoundParameters)
}
