param(
  [Parameter(Mandatory = $true)]
  [string]$CandidateInstaller,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[A-Za-z0-9._-]{1,80}$')]
  [string]$Label,
  [switch]$VerifyLongRunningUpdateBanner
)

$ErrorActionPreference = 'Stop'
$repository = 'opensquilla/opensquilla'
$oldTag = 'v0.5.0rc3'
$oldAsset = 'OpenSquilla-0.5.0-rc3-win-x64.exe'
$candidate = (Resolve-Path -LiteralPath $CandidateInstaller).Path
$candidateName = [IO.Path]::GetFileName($candidate)
$sandbox = Join-Path $env:RUNNER_TEMP "opensquilla-release-preservation-$Label"
$oldDir = Join-Path $sandbox 'rc3'
$installDir = Join-Path $sandbox 'OpenSquilla'
$appData = Join-Path $sandbox 'appdata'
$userData = Join-Path $appData 'OpenSquilla'
$profile = Join-Path $userData 'opensquilla'
$probe = Join-Path $PWD '.github\scripts\verify-release-profile-preservation.py'
$updateBannerSmoke = Join-Path $PWD 'desktop\electron\scripts\test-packaged-update-banner.mjs'
$env:APPDATA = $appData
$env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE = '1'
$env:OPENSQUILLA_RECOVERY_OFFLINE = '1'

New-Item -ItemType Directory -Force -Path $oldDir, $appData | Out-Null
gh release download $oldTag --repo $repository --pattern $oldAsset --dir $oldDir
if ($LASTEXITCODE -ne 0) { throw 'Failed to download the RC3 Windows installer.' }
$oldInstaller = Join-Path $oldDir $oldAsset

function Stop-InstalledProcesses {
  Get-Process -Name 'OpenSquilla', 'opensquilla-gateway' -ErrorAction SilentlyContinue |
    ForEach-Object {
      try {
        $path = if ($_.Path) { [IO.Path]::GetFullPath($_.Path) } else { '' }
        $prefix = [IO.Path]::GetFullPath($installDir + [IO.Path]::DirectorySeparatorChar)
        if ($path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
          & taskkill.exe /PID $_.Id /T /F 2>$null | Out-Null
        }
      } catch {
        if ($_.Exception.Message -notmatch 'exited|cannot find|No process') { throw }
      }
    }
}

try {
  $old = Start-Process -FilePath $oldInstaller -ArgumentList @('/S', "/D=$installDir") `
    -Wait -PassThru
  if ($old.ExitCode -ne 0) { throw "RC3 installer failed with exit code $($old.ExitCode)." }

  python $probe seed --home $profile --label $Label
  if ($LASTEXITCODE -ne 0) { throw 'Failed to seed the synthetic RC3 profile.' }

  $installed = Start-Process -FilePath $candidate -ArgumentList @('/S', "/D=$installDir") `
    -Wait -PassThru
  if ($installed.ExitCode -ne 0) {
    throw "Candidate installer failed with exit code $($installed.ExitCode)."
  }
  python $probe verify --home $profile --label $Label
  if ($LASTEXITCODE -ne 0) { throw 'Candidate installation changed RC3 profile data.' }

  $app = Join-Path $installDir 'OpenSquilla.exe'
  if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
    throw 'Candidate installation did not publish OpenSquilla.exe.'
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
  python $probe verify --home $profile --label $Label
  if ($LASTEXITCODE -ne 0) { throw 'Candidate launch changed RC3 profile data.' }

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
  python $probe verify --home $profile --label $Label
  if ($LASTEXITCODE -ne 0) { throw 'Candidate uninstaller changed RC3 profile data.' }
} finally {
  Stop-InstalledProcesses
}
