param(
  [Parameter(Mandatory = $true)]
  [string]$CandidateInstaller,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[A-Za-z0-9._-]{1,80}$')]
  [string]$Label,
  [switch]$VerifyLongRunningUpdateBanner,
  [string]$RealUpdateChannelManifest = '',
  [ValidateSet('custom', 'default')]
  [string]$InstallMode = 'custom'
)

$ErrorActionPreference = 'Stop'
$repository = 'opensquilla/opensquilla'
$oldTag = 'v0.5.3'
$oldAsset = 'OpenSquilla-0.5.3-win-x64.exe'
$candidate = (Resolve-Path -LiteralPath $CandidateInstaller).Path
$candidateName = [IO.Path]::GetFileName($candidate)
$sandbox = Join-Path $env:RUNNER_TEMP "opensquilla-release-preservation-$Label-$InstallMode"
$oldDir = Join-Path $sandbox 'v0.5.3'
$appData = Join-Path $sandbox 'appdata'
$localAppData = Join-Path $sandbox 'localappdata'
$userData = Join-Path $appData 'OpenSquilla'
$profile = Join-Path $userData 'opensquilla'
$probe = Join-Path $PWD '.github\scripts\verify-release-profile-preservation.py'
$updateBannerSmoke = Join-Path $PWD 'desktop\electron\scripts\test-packaged-update-banner.mjs'
$realUpdateDriver = Join-Path $PWD 'desktop\electron\scripts\test-packaged-real-update-flow.mjs'
$realUpdateResult = Join-Path $sandbox 'real-update-result.json'
$externalSentinels = Join-Path $sandbox 'synthetic-system-tools'
$expectedInstalledVersion = ''
$env:APPDATA = $appData
$env:LOCALAPPDATA = $localAppData
$env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE = '1'
$env:OPENSQUILLA_RECOVERY_OFFLINE = '1'

New-Item -ItemType Directory -Force -Path $oldDir, $appData, $localAppData | Out-Null
$installDir = if ($InstallMode -eq 'custom') { Join-Path $sandbox 'OpenSquilla' } else { '' }
if ($RealUpdateChannelManifest) {
  $RealUpdateChannelManifest = (Resolve-Path -LiteralPath $RealUpdateChannelManifest).Path
  $rehearsalManifest = Get-Content -LiteralPath $RealUpdateChannelManifest -Raw |
    ConvertFrom-Json
  $expectedInstalledVersion = [string]$rehearsalManifest.version
  if ($expectedInstalledVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "Stable updater rehearsal has an invalid expected version: $expectedInstalledVersion"
  }
}
gh release download $oldTag --repo $repository --pattern $oldAsset --dir $oldDir
if ($LASTEXITCODE -ne 0) { throw 'Failed to download the v0.5.3 Windows installer.' }
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

try {
  $oldArguments = @('/S')
  if ($InstallMode -eq 'custom') { $oldArguments += "/D=$installDir" }
  $old = Start-Process -FilePath $oldInstaller -ArgumentList $oldArguments `
    -Wait -PassThru
  if ($old.ExitCode -ne 0) { throw "v0.5.3 installer failed with exit code $($old.ExitCode)." }

  if ($InstallMode -eq 'default') {
    $oldApp = Get-ChildItem -LiteralPath $localAppData -Filter 'OpenSquilla.exe' -File -Recurse |
      Select-Object -First 1
    if (-not $oldApp) { throw 'v0.5.3 default installation did not publish OpenSquilla.exe.' }
    $installDir = $oldApp.Directory.FullName
  }

  $oldRuntime = Join-Path $installDir 'resources\runtime\developer\windows-x64'
  foreach ($oldExecutable in @(
    (Join-Path $oldRuntime 'python\python.exe'),
    (Join-Path $oldRuntime 'node\node.exe'),
    (Join-Path $oldRuntime 'git-bash\bin\bash.exe')
  )) {
    if (-not (Test-Path -LiteralPath $oldExecutable -PathType Leaf)) {
      throw "v0.5.3 bundled runtime is missing: $oldExecutable"
    }
  }

  python $probe seed --home $profile --label $Label --external-root $externalSentinels
  if ($LASTEXITCODE -ne 0) { throw 'Failed to seed the synthetic v0.5.3 profile.' }

  if ($RealUpdateChannelManifest) {
    # Gate boundary: this proves updater discovery/download integrity, behavior while
    # v0.5.3 is running, successful normal NSIS handoff, and post-install preservation.
    # electron-builder's NSIS upgrade is not transactional after the old uninstaller
    # starts; disk, power, or extraction failures in that later window are out of scope.
    $candidateSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash.ToLowerInvariant()
    $driverArguments = @(
      $realUpdateDriver,
      '--executable', (Join-Path $installDir 'OpenSquilla.exe'),
      '--user-data-dir', $userData,
      '--channel-manifest', $RealUpdateChannelManifest,
      '--expected-version', $expectedInstalledVersion,
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
    if ($LASTEXITCODE -ne 0) { throw 'Official v0.5.3 real updater rehearsal failed.' }
    $updateResult = Get-Content -LiteralPath $realUpdateResult -Raw | ConvertFrom-Json
    if (
      -not $updateResult.ok -or
      $updateResult.fromVersion -ne '0.5.3' -or
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
      throw 'Official v0.5.3 downloaded installer bytes differ from the Draft candidate.'
    }
  } else {
    $installed = Start-Process -FilePath $candidate -ArgumentList @('/S', "/D=$installDir") `
      -Wait -PassThru
    if ($installed.ExitCode -ne 0) {
      throw "Candidate installer failed with exit code $($installed.ExitCode)."
    }
  }
  python $probe verify --home $profile --label $Label --external-root $externalSentinels
  if ($LASTEXITCODE -ne 0) { throw 'Candidate installation changed v0.5.3 profile data.' }

  $candidateRuntime = Join-Path $installDir 'resources\runtime'
  if (Test-Path -LiteralPath (Join-Path $candidateRuntime 'developer')) {
    throw 'Candidate installation retained the v0.5.3 bundled developer runtimes.'
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
  if ($expectedInstalledVersion) {
    $actualProductVersion = ([Diagnostics.FileVersionInfo]::GetVersionInfo($app)).ProductVersion
    if (-not $actualProductVersion) {
      throw 'Installed OpenSquilla.exe does not declare a ProductVersion.'
    }
    $actualProductVersion = $actualProductVersion.Trim()
    if ($actualProductVersion -ne $expectedInstalledVersion) {
      throw (
        "Installed OpenSquilla.exe ProductVersion $actualProductVersion does not match " +
        "the rehearsed version $expectedInstalledVersion."
      )
    }
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
  python $probe verify --home $profile --label $Label --external-root $externalSentinels
  if ($LASTEXITCODE -ne 0) { throw 'Candidate launch changed v0.5.3 profile data.' }

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
  python $probe verify --home $profile --label $Label --external-root $externalSentinels
  if ($LASTEXITCODE -ne 0) { throw 'Candidate uninstaller changed v0.5.3 profile data.' }
} finally {
  Stop-InstalledProcesses
}
