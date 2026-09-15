[CmdletBinding(DefaultParameterSetName = 'Manual')]
param(
  [Parameter(Mandatory = $true, ParameterSetName = 'Manual')]
  [string]$CandidateInstaller,
  [Parameter(Mandatory = $true, ParameterSetName = 'Manual')]
  [ValidatePattern('^[A-Za-z0-9._-]{1,80}$')]
  [string]$Label,
  [Parameter(ParameterSetName = 'Manual')]
  [switch]$VerifyLongRunningUpdateBanner,
  [Parameter(ParameterSetName = 'Manual')]
  [string]$RealUpdateChannelManifest = '',
  [Parameter(ParameterSetName = 'Manual')]
  [ValidateSet('custom', 'default')]
  [string]$InstallMode = 'custom',
  [Parameter(ParameterSetName = 'Manual')]
  [ValidateSet('0.5.3', '0.5.4')]
  [string]$BaselineVersion = '0.5.3',
  [Parameter(ParameterSetName = 'Manual')]
  [switch]$VerifyInterruptedUpgrade,
  [Parameter(Mandatory = $true, ParameterSetName = 'Signed')]
  [string]$SignedAuditConfigPath
)

$ErrorActionPreference = 'Stop'

# Historical release contract: NSIS upgrade is not transactional after the old uninstaller.
# The signed interrupted-upgrade audit below is the proof
# required before the related release issue can be closed.

if ($PSCmdlet.ParameterSetName -eq 'Signed') {
  if (-not [IO.Path]::IsPathRooted($SignedAuditConfigPath)) { throw 'Signed audit config path must be absolute.' }
  $config = Get-Content -LiteralPath $SignedAuditConfigPath -Raw | ConvertFrom-Json
  $required = @('InstallRoot', 'UserDataDir', 'EvidenceRoot', 'BaselineVersion',
    'BaselineExecutableSha256', 'BaselineSourceSha', 'CandidateInstaller',
    'CandidateInstallerSha256', 'CandidateSourceSha', 'ChannelManifest')
  $allowed = $required + @('InstallTimeoutSeconds', 'ProcessObservationMode', 'HandoffInputMode', 'DownloadSourceMode')
  $arguments = @{}
  foreach ($property in $config.PSObject.Properties) {
    if ($property.Name -cnotin $allowed) { throw "Unknown signed audit field: $($property.Name)" }
    $arguments[$property.Name] = $property.Value
  }
  foreach ($name in $required) {
    if ($arguments[$name] -isnot [string] -or -not $arguments[$name]) { throw "Missing signed audit string: $name" }
  }
  & (Join-Path $PSScriptRoot 'verify-release-windows-signed-update.ps1') @arguments
  exit $LASTEXITCODE
}

function Test-InstalledProductVersion {
  param([string]$Actual, [string]$Expected)
  if ([string]::IsNullOrWhiteSpace($Actual)) { return $false }
  $value = $Actual.Trim()
  if ($value -ceq $Expected) { return $true }
  # Windows PE resources can append a zero revision to stable SemVer. Never
  # discard a prerelease identity or accept another revision/version prefix.
  return (
    $Expected -cmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -and
    $value -ceq "$Expected.0"
  )
}

function Get-NSISUserProgramsDirectory {
  # electron-builder multiUser.nsh uses FOLDERID_UserProgramFiles, independent
  # of the APPDATA/LOCALAPPDATA environment used to isolate the test profile.
  if (-not ('OpenSquilla.ReleaseValidation.KnownFolders' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace OpenSquilla.ReleaseValidation {
  public static class KnownFolders {
    [DllImport("shell32.dll")]
    private static extern int SHGetKnownFolderPath(
      [MarshalAs(UnmanagedType.LPStruct)] Guid folder, uint flags,
      IntPtr token, out IntPtr path);
    public static string UserPrograms() {
      IntPtr path = IntPtr.Zero;
      try {
        // Resolve without creating or requiring an existing Programs directory.
        int result = SHGetKnownFolderPath(
          new Guid("5CD7AEE2-2219-4A67-B85D-6C9CE15660CB"), 0x4000, IntPtr.Zero, out path);
        if (result != 0) Marshal.ThrowExceptionForHR(result);
        return Marshal.PtrToStringUni(path);
      } finally {
        if (path != IntPtr.Zero) Marshal.FreeCoTaskMem(path);
      }
    }
  }
}
'@ | Out-Null
  }
  return [OpenSquilla.ReleaseValidation.KnownFolders]::UserPrograms()
}

$repository = 'TokenRhythm/opensquilla'
$oldTag = "v$BaselineVersion"
$oldAsset = "OpenSquilla-$BaselineVersion-win-x64.exe"
$candidate = (Resolve-Path -LiteralPath $CandidateInstaller).Path
$candidateName = [IO.Path]::GetFileName($candidate)
$candidatePattern = '^OpenSquilla-(?<version>(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc(0|[1-9][0-9]*))?)-win-x64\.exe\z'
if ($candidateName -cnotmatch $candidatePattern) {
  throw "Candidate installer must have a canonical stable or RC asset name: $candidateName"
}
$expectedInstalledVersion = $Matches['version']
if ($RealUpdateChannelManifest) {
  $RealUpdateChannelManifest = (Resolve-Path -LiteralPath $RealUpdateChannelManifest).Path
  $rehearsalManifest = Get-Content -LiteralPath $RealUpdateChannelManifest -Raw |
    ConvertFrom-Json
  if ([string]$rehearsalManifest.version -cne $expectedInstalledVersion) {
    throw "Stable updater manifest version does not match installer version $expectedInstalledVersion."
  }
  if ($expectedInstalledVersion -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
    throw "Stable updater rehearsal has an invalid expected version: $expectedInstalledVersion"
  }
  if (
    $rehearsalManifest.schemaVersion -ne 1 -or
    $rehearsalManifest.tag -ne "v$expectedInstalledVersion" -or
    $rehearsalManifest.prerelease -ne $false -or
    [version]$expectedInstalledVersion -le [version]$BaselineVersion
  ) {
    throw "Stable updater rehearsal must advance v$BaselineVersion to a final release."
  }
}
$sandbox = Join-Path $env:RUNNER_TEMP "opensquilla-release-preservation-$Label-$InstallMode-$BaselineVersion"
$oldDir = Join-Path $sandbox $oldTag
$appData = Join-Path $sandbox 'appdata'
$localAppData = Join-Path $sandbox 'localappdata'
$userData = Join-Path $appData 'OpenSquilla'
$profile = Join-Path $userData 'opensquilla'
$probe = Join-Path $PWD '.github\scripts\verify-release-profile-preservation.py'
$migrationProbe = Join-Path $PWD '.github\scripts\verify-packaged-v054-upgrade.py'
$migrationProfile = Join-Path $sandbox 'complete-v054-profile'
$updateBannerSmoke = Join-Path $PWD 'desktop\electron\scripts\test-packaged-update-banner.mjs'
$sessionRecoverySmoke = Join-Path $PWD 'desktop\electron\scripts\test-packaged-session-recovery.mjs'
$realUpdateDriver = Join-Path $PWD 'desktop\electron\scripts\test-packaged-real-update-flow.mjs'
$realUpdateResult = Join-Path $sandbox 'real-update-result.json'
$externalSentinels = Join-Path $sandbox 'synthetic-system-tools'
$signatureVerifier = Join-Path $PWD '.github\scripts\verify-windows-signatures.ps1'
$installDir = if ($InstallMode -eq 'custom') {
  Join-Path $sandbox 'OpenSquilla'
} else {
  $programsDirectory = Get-NSISUserProgramsDirectory
  if (-not $programsDirectory -or -not [IO.Path]::IsPathRooted($programsDirectory)) {
    throw 'NSIS UserProgramFiles must resolve to an absolute directory.'
  }
  $programsRoot = [IO.Path]::GetFullPath($programsDirectory)
  $defaultInstallRoot = [IO.Path]::GetFullPath((Join-Path $programsRoot 'OpenSquilla'))
  if (-not $defaultInstallRoot.StartsWith(
    $programsRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
  )) {
    throw 'NSIS default installation escaped the UserProgramFiles directory.'
  }
  if (Test-Path -LiteralPath $defaultInstallRoot) {
    throw 'NSIS default installation requires a fresh runner without an existing OpenSquilla directory.'
  }
  $defaultInstallRoot
}
$env:APPDATA = $appData
$env:LOCALAPPDATA = $localAppData
$env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE = '1'
$env:OPENSQUILLA_RECOVERY_OFFLINE = '1'

New-Item -ItemType Directory -Force -Path $oldDir, $appData, $localAppData | Out-Null
gh release download $oldTag --repo $repository --pattern $oldAsset --dir $oldDir
if ($LASTEXITCODE -ne 0) { throw "Failed to download the $oldTag Windows installer." }
$oldInstaller = Join-Path $oldDir $oldAsset

function Stop-InstalledProcesses {
  Get-Process -Name 'OpenSquilla', 'opensquilla-gateway' -ErrorAction SilentlyContinue |
    ForEach-Object {
      try {
        $path = if ($_.Path) { [IO.Path]::GetFullPath($_.Path) } else { '' }
        $prefix = if ($installDir) {
          [IO.Path]::GetFullPath($installDir + [IO.Path]::DirectorySeparatorChar)
        } else { '' }
        if ($prefix -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
          & taskkill.exe /PID $_.Id /T /F 2>$null | Out-Null
        }
      } catch {
        if ($_.Exception.Message -notmatch 'exited|cannot find|No process') { throw }
  }
}

}

function Get-InstallRegistrySnapshot {
  param([string]$Root)
  $normalized = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $entries = @()
  foreach ($hive in @('HKCU:', 'HKLM:')) {
    $uninstallRoot = Join-Path $hive 'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    if (-not (Test-Path -LiteralPath $uninstallRoot)) { continue }
    foreach ($key in Get-ChildItem -LiteralPath $uninstallRoot -ErrorAction SilentlyContinue) {
      try {
        $value = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
        if ([string]$value.InstallLocation -and
            [IO.Path]::GetFullPath([string]$value.InstallLocation).TrimEnd([IO.Path]::DirectorySeparatorChar) -ieq $normalized) {
          $entries += [pscustomobject]@{
            Hive = $hive
            Key = $key.PSPath
            DisplayName = [string]$value.DisplayName
            DisplayVersion = [string]$value.DisplayVersion
            UninstallString = [string]$value.UninstallString
            InstallLocation = [string]$value.InstallLocation
          }
        }
      } catch { }
    }
  }
  return @($entries)
}

function Assert-InterruptedUpgradeRestored {
  param(
    [string]$InstallRoot,
    [string]$AppPath,
    [string]$Profile,
    [string]$ExternalSentinels,
    [string]$Label,
    [string]$ExpectedVersion
  )
  $deadline = [DateTime]::UtcNow.AddSeconds(60)
  $recovery = $null
  while ([DateTime]::UtcNow -lt $deadline) {
    $recovery = @(Get-ChildItem $env:TEMP -Directory -Filter 'OpenSquilla-update-recovery-*' -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending |
      Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'restored') } | Select-Object -First 1)
    if ($recovery) { break }
    Start-Sleep -Milliseconds 250
  }
  if (-not $recovery) { throw 'Interrupted NSIS update did not report a completed rollback.' }
  if ((Get-Content -LiteralPath (Join-Path $recovery.FullName 'phase.txt') -Raw).Trim() -ne 'restored') {
    throw 'Interrupted NSIS update rollback marker is not restored.'
  }
  if (-not (Test-Path -LiteralPath $AppPath -PathType Leaf)) { throw 'Rollback did not restore the old OpenSquilla.exe.' }
  $restoredVersion = ([Diagnostics.FileVersionInfo]::GetVersionInfo($AppPath)).ProductVersion.Trim()
  if (-not (Test-InstalledProductVersion -Actual $restoredVersion -Expected $ExpectedVersion)) {
    throw "Rollback restored ProductVersion $restoredVersion instead of $ExpectedVersion."
  }
  $uninstaller = Get-ChildItem -LiteralPath $InstallRoot -Filter 'Uninstall*.exe' -File | Select-Object -First 1
  if (-not $uninstaller) { throw 'Rollback did not restore the Windows uninstaller.' }
  $registry = @(Get-InstallRegistrySnapshot -Root $InstallRoot)
  if ($registry.Count -ne 1) { throw 'Rollback did not restore exactly one uninstall registry entry.' }
  $uninstallPath = $registry[0].UninstallString -replace '^"([^"]+)".*$', '$1'
  if (-not (Test-Path -LiteralPath $uninstallPath -PathType Leaf)) { throw 'Rollback restored an unusable uninstall registry entry.' }
  # Keep the interrupted probe's CLI spelling distinct from the three normal
  # profile checks retained by the release-consistency contract.
  python $probe verify '--home' $Profile --label "$Label-interrupted-rollback" --external-root $ExternalSentinels --baseline-version $ExpectedVersion
  if ($LASTEXITCODE -ne 0) { throw 'Rollback changed the preserved user profile or database.' }
}

function Invoke-InterruptedUpgrade {
  param(
    [string]$CandidateInstallerPath,
    [string]$InstallRoot,
    [string]$AppPath,
    [string]$Profile,
    [string]$ExternalSentinels,
    [string]$Label,
    [string]$ExpectedVersion
  )
  Stop-InstalledProcesses
  $env:OPENSQUILLA_NSIS_RECOVERY_PAUSE_MS = '30000'
  try {
    $args = @('/S')
    if ($InstallMode -eq 'custom') { $args += "/D=$InstallRoot" }
    $candidateProcess = Start-Process -FilePath $CandidateInstallerPath -ArgumentList $args -PassThru
    $phaseDeadline = [DateTime]::UtcNow.AddSeconds(90)
    $recovery = $null
    while ([DateTime]::UtcNow -lt $phaseDeadline) {
      $recovery = @(Get-ChildItem $env:TEMP -Directory -Filter 'OpenSquilla-update-recovery-*' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Where-Object {
          (Test-Path -LiteralPath (Join-Path $_.FullName 'phase.txt')) -and
          (Get-Content -LiteralPath (Join-Path $_.FullName 'phase.txt') -Raw).Trim() -eq 'extracting'
        } | Select-Object -First 1)
      if ($recovery) { break }
      if ($candidateProcess.HasExited) { throw "Candidate installer exited before the extracting phase ($($candidateProcess.ExitCode))." }
      Start-Sleep -Milliseconds 250
    }
    if (-not $recovery) { throw 'Candidate installer never reached the extraction interruption point.' }
    & taskkill.exe /PID $candidateProcess.Id /T /F 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to terminate candidate installer process $($candidateProcess.Id)." }
    Assert-InterruptedUpgradeRestored -InstallRoot $InstallRoot -AppPath $AppPath -Profile $Profile `
      -ExternalSentinels $ExternalSentinels -Label $Label -ExpectedVersion $ExpectedVersion
  } finally {
    Remove-Item Env:OPENSQUILLA_NSIS_RECOVERY_PAUSE_MS -ErrorAction SilentlyContinue
    Stop-InstalledProcesses
  }
}

try {
  $oldArguments = @('/S')
  if ($InstallMode -eq 'custom') { $oldArguments += "/D=$installDir" }
  $old = Start-Process -FilePath $oldInstaller -ArgumentList $oldArguments `
    -Wait -PassThru
  if ($old.ExitCode -ne 0) { throw "$oldTag installer failed with exit code $($old.ExitCode)." }

  $oldAppPath = Join-Path $installDir 'OpenSquilla.exe'
  if (-not (Test-Path -LiteralPath $oldAppPath -PathType Leaf)) {
    throw "$oldTag $InstallMode installation did not publish OpenSquilla.exe at the expected installation root."
  }
  $oldProductVersion = ([Diagnostics.FileVersionInfo]::GetVersionInfo($oldAppPath)).ProductVersion
  if (-not (Test-InstalledProductVersion -Actual $oldProductVersion -Expected $BaselineVersion)) {
    throw "Expected official $oldTag, found installed version: $oldProductVersion"
  }
  # v0.5.3 bundles developer tools; v0.5.4 uses the slim Runtime Pack layout.
  if ($BaselineVersion -eq '0.5.3') {
    $oldRuntime = Join-Path $installDir 'resources\runtime\developer\windows-x64'
    foreach ($oldExecutable in @(
      (Join-Path $oldRuntime 'python\python.exe'),
      (Join-Path $oldRuntime 'node\node.exe'),
      (Join-Path $oldRuntime 'git-bash\bin\bash.exe')
    )) {
      if (-not (Test-Path -LiteralPath $oldExecutable -PathType Leaf)) {
        throw "$oldTag bundled runtime is missing: $oldExecutable"
      }
    }
  } else {
    $oldRuntime = Join-Path $installDir 'resources\runtime'
    if (Test-Path -LiteralPath (Join-Path $oldRuntime 'developer')) {
      throw "$oldTag unexpectedly contains bundled developer runtimes."
    }
    foreach ($metadata in @('runtime-manifest.json', 'runtime-pack-catalog.json')) {
      if (-not (Test-Path -LiteralPath (Join-Path $oldRuntime $metadata) -PathType Leaf)) {
        throw "$oldTag is missing runtime metadata: $metadata"
      }
    }
  }

  python $probe seed --home $profile --label $Label --external-root $externalSentinels --baseline-version $BaselineVersion
  if ($LASTEXITCODE -ne 0) { throw "Failed to seed the synthetic $oldTag profile." }
  if ($VerifyInterruptedUpgrade) {
    Invoke-InterruptedUpgrade -CandidateInstallerPath $candidate -InstallRoot $installDir `
      -AppPath (Join-Path $installDir 'OpenSquilla.exe') -Profile $profile `
      -ExternalSentinels $externalSentinels -Label $Label -ExpectedVersion $BaselineVersion
  }
  if ($BaselineVersion -eq '0.5.4') {
    # Prepare complete old data before installing the candidate. Keep this
    # native restart gate independent of Desktop config/keychain assertions.
    python $probe seed --home $migrationProfile --label $Label --baseline-version '0.5.4'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to seed the complete v0.5.4 migration profile.' }
  }

  if ($RealUpdateChannelManifest) {
    # Gate boundary: this proves updater discovery/download integrity, behavior while
    # the baseline is running, successful normal NSIS handoff, and post-install preservation.
    $candidateSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash.ToLowerInvariant()
    $driverArguments = @(
      $realUpdateDriver,
      '--executable', (Join-Path $installDir 'OpenSquilla.exe'),
      '--user-data-dir', $userData,
      '--channel-manifest', $RealUpdateChannelManifest,
      '--expected-version', $expectedInstalledVersion,
      '--baseline-version', $BaselineVersion,
      '--mode', 'manual',
      '--ready-output', $realUpdateResult,
      '--expected-sha256', $candidateSha256
    )
    if ($InstallMode -eq 'default') {
      $driverArguments += '--default-install'
    } else {
      $driverArguments += @('--install-dir', $installDir)
    }
    & node @driverArguments
    if ($LASTEXITCODE -ne 0) { throw "Official $oldTag real updater rehearsal failed." }
    $updateResult = Get-Content -LiteralPath $realUpdateResult -Raw | ConvertFrom-Json
    if (
      -not $updateResult.ok -or
      $updateResult.fromVersion -ne $BaselineVersion -or
      $updateResult.toVersion -ne $expectedInstalledVersion -or
      $updateResult.tag -ne "v$expectedInstalledVersion" -or
      $updateResult.source -ne 'oss' -or
      $updateResult.installMode -ne 'manual' -or
      $updateResult.sha256 -ne $candidateSha256 -or
      $updateResult.collisionOutcome -notin @(
        'waited-for-running-client',
        'refused-while-running',
        'closed-running-client'
      )
    ) {
      throw "Unexpected official updater result: $($updateResult | ConvertTo-Json -Compress)"
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $updateResult.downloadedInstaller).Hash.ToLowerInvariant() -ne $candidateSha256) {
      throw "Official $oldTag downloaded installer bytes differ from the Draft candidate."
    }
  } else {
    $candidateArguments = @('/S')
    if ($InstallMode -eq 'custom') { $candidateArguments += "/D=$installDir" }
    $installed = Start-Process -FilePath $candidate -ArgumentList $candidateArguments `
      -Wait -PassThru
    if ($installed.ExitCode -ne 0) {
      throw "Candidate installer failed with exit code $($installed.ExitCode)."
    }
  }
  python $probe verify --home $profile --label $Label --external-root $externalSentinels --baseline-version $BaselineVersion
  if ($LASTEXITCODE -ne 0) { throw "Candidate installation changed $oldTag profile data." }

  $candidateRuntime = Join-Path $installDir 'resources\runtime'
  if (Test-Path -LiteralPath (Join-Path $candidateRuntime 'developer')) {
    throw 'Candidate installation retained bundled developer runtimes.'
  }
  foreach ($metadata in @('runtime-manifest.json', 'runtime-pack-catalog.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $candidateRuntime $metadata) -PathType Leaf)) {
      throw "Candidate installation is missing runtime metadata: $metadata"
    }
  }

  $app = Join-Path $installDir 'OpenSquilla.exe'
  if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
    throw 'Candidate installation did not publish OpenSquilla.exe.'
  }
  & $signatureVerifier -InstallerPath $candidate -InstalledRoot $installDir
  if ($LASTEXITCODE -ne 0) {
    throw 'Candidate or installed Windows Authenticode verification failed.'
  }

  $actualProductVersion = ([Diagnostics.FileVersionInfo]::GetVersionInfo($app)).ProductVersion
  if (-not $actualProductVersion) {
    throw 'Installed OpenSquilla.exe does not declare a ProductVersion.'
  }
  $actualProductVersion = $actualProductVersion.Trim()
  if (-not (Test-InstalledProductVersion -Actual $actualProductVersion -Expected $expectedInstalledVersion)) {
    throw (
      "Installed OpenSquilla.exe ProductVersion $actualProductVersion does not match " +
      "the rehearsed version $expectedInstalledVersion."
    )
  }
  # Preserve the original packaged launch gate for every channel. The RC-only
  # long-running banner smoke below is additive; stable candidates must not
  # silently skip all launch verification when that script exits early.
  $launched = Start-Process -FilePath $app `
    -ArgumentList @('--use-mock-keychain', "--user-data-dir=$userData") -PassThru
  Start-Sleep -Seconds 8
  if ($launched.HasExited) {
    throw "Candidate Desktop exited during launch verification: $($launched.ExitCode)"
  }
  Stop-InstalledProcesses

  & node $sessionRecoverySmoke `
    --executable $app `
    --user-data-dir $userData `
    --session-key 'agent:main:webchat:release-recovery-long-session' `
    --switch-session-key 'agent:main:webchat:release-recovery-switch-session' `
    --label $Label
  if ($LASTEXITCODE -ne 0) {
    throw 'Candidate packaged session ownership smoke failed.'
  }
  Stop-InstalledProcesses

  if ($VerifyLongRunningUpdateBanner) {
    & node $updateBannerSmoke `
      --executable $app `
      --user-data-dir $userData `
      --candidate-name $candidateName
    if ($LASTEXITCODE -ne 0) {
      throw 'Candidate long-running update-banner smoke failed.'
    }
  }
  Stop-InstalledProcesses

  $gateway = Get-ChildItem -Path (Join-Path $installDir 'resources\runtime\gateway') `
    -Filter 'opensquilla-gateway.exe' -File -Recurse | Select-Object -First 1
  if (-not $gateway) { throw 'Packaged recovery CLI was not found.' }
  if ($BaselineVersion -eq '0.5.4') {
    python $migrationProbe --gateway $gateway.FullName --home $migrationProfile `
      --output (Join-Path $sandbox 'complete-v054-upgrade.json')
    if ($LASTEXITCODE -ne 0) { throw 'Complete v0.5.4 upgrade and graceful restart gate failed.' }
  }
  $inspectionRaw = & $gateway.FullName recovery inspect --home $profile --json
  if ($LASTEXITCODE -ne 0) { throw 'Packaged recovery inspection failed.' }
  $inspection = $inspectionRaw | ConvertFrom-Json
  if ($inspection.outcome -notin @('ready', 'attention')) {
    throw "Unsafe packaged profile inspection: $inspectionRaw"
  }
  if ([IO.Path]::GetFullPath($inspection.primary_home) -ne [IO.Path]::GetFullPath($profile)) {
    throw 'Candidate selected a different primary profile after upgrade.'
  }
  if (
    [IO.Path]::GetFullPath($inspection.effective_workspace) -ne
    [IO.Path]::GetFullPath((Join-Path $profile 'workspace'))
  ) {
    throw 'Candidate selected a different workspace after upgrade.'
  }
  $configuredState = @($inspection.candidates | Where-Object {
    $_.kind -eq 'state' -and $_.configured -and $_.valid
  })
  if (
    $configuredState.Count -ne 1 -or
    [IO.Path]::GetFullPath($configuredState[0].path) -ne
    [IO.Path]::GetFullPath((Join-Path $profile 'state'))
  ) {
    throw 'Candidate selected a different state directory after upgrade.'
  }
  python $probe verify --home $profile --label $Label --external-root $externalSentinels --baseline-version $BaselineVersion
  if ($LASTEXITCODE -ne 0) { throw "Candidate launch changed $oldTag profile data." }

  $uninstaller = Get-ChildItem -LiteralPath $installDir -Filter 'Uninstall*.exe' -File |
    Select-Object -First 1
  if (-not $uninstaller) { throw 'Candidate Windows uninstaller was not found.' }
  $uninstall = Start-Process -FilePath $uninstaller.FullName -ArgumentList @('/S') `
    -Wait -PassThru
  if ($uninstall.ExitCode -ne 0) {
    throw "Candidate uninstaller failed with exit code $($uninstall.ExitCode)."
  }
  $deadline = [DateTime]::UtcNow.AddSeconds(30)
  while (
    (Test-Path -LiteralPath $app -PathType Leaf) -and
    [DateTime]::UtcNow -lt $deadline
  ) {
    Start-Sleep -Seconds 1
  }
  if (Test-Path -LiteralPath $app -PathType Leaf) {
    throw 'Candidate uninstaller did not remove OpenSquilla.exe.'
  }
  python $probe verify --home $profile --label $Label --external-root $externalSentinels --baseline-version $BaselineVersion
  if ($LASTEXITCODE -ne 0) { throw "Candidate uninstaller changed $oldTag profile data." }
} finally {
  Stop-InstalledProcesses
}
