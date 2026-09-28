[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$CandidateInstaller,
  [Parameter(Mandatory = $true)][string]$CandidateManifest,
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceSha,
  [ValidatePattern('^([0-9]+)?$')][string]$SourceRunId = '',
  [ValidatePattern('^([0-9a-f]{64})?$')][string]$InstallerSha256 = '',
  [Parameter(Mandatory = $true)][ValidateSet('startup-compat', 'ownership', 'migration', 'session')][string]$Probe,
  [switch]$RequireSignature
)

$ErrorActionPreference = 'Stop'
# NSIS changes installer registration: only disposable CI workers may run this
# controller. Native developer checks invoke the unpacked probes directly.
if ($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) {
  throw 'Candidate installation probes require a disposable GitHub Actions runner.'
}
$candidate = (Resolve-Path -LiteralPath $CandidateInstaller).Path
$manifest = (Resolve-Path -LiteralPath $CandidateManifest).Path
$context = .github/scripts/windows-candidate-context.ps1 -SourceSha $SourceSha -SourceRunId $SourceRunId
$identityArgs = @('--installer', $candidate, '--manifest', $manifest, '--source-sha', $SourceSha,
  '--workflow-sha', $context.WorkflowSha, '--expected-version', $context.Version)
if ($InstallerSha256) { $identityArgs += @('--installer-sha256', $InstallerSha256) }
python .github/scripts/windows_candidate_identity.py @identityArgs
if ($LASTEXITCODE -ne 0) { throw 'Candidate provenance verification failed.' }
if ($RequireSignature) {
  .github/scripts/verify-windows-signatures.ps1 -InstallerPath $candidate
  if ($LASTEXITCODE -ne 0) { throw 'Candidate signature verification failed.' }
}
$stage = Join-Path $env:RUNNER_TEMP "candidate-$Probe"
if (Test-Path -LiteralPath $stage) { throw 'Probe requires a new isolated staging directory.' }
$installRoot = Join-Path $stage 'app'
$evidence = Join-Path $stage 'evidence'
New-Item -ItemType Directory -Path $installRoot, $evidence | Out-Null
$installed = $false
try {
  $install = Start-Process -FilePath $candidate -ArgumentList '/S', "/D=$installRoot" -WindowStyle Hidden -PassThru
  if (-not $install.WaitForExit(180000)) { throw 'Candidate installation deadline exceeded.' }
  if ($install.ExitCode -ne 0) { throw "Candidate installer exit $($install.ExitCode)." }
  $installed = $true
  $installedVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $installRoot 'OpenSquilla.exe')).ProductVersion
  python .github/scripts/windows_candidate_identity.py @identityArgs --root $installRoot --installed-version $installedVersion
  if ($LASTEXITCODE -ne 0) { throw 'Installed candidate bytes differ from the manifest.' }
  if ($RequireSignature) {
    .github/scripts/verify-windows-signatures.ps1 -InstalledRoot $installRoot
    if ($LASTEXITCODE -ne 0) { throw 'Installed candidate signature verification failed.' }
  }
  Copy-Item -LiteralPath $manifest -Destination (Join-Path $evidence 'audit-candidate.json')
  $app = Join-Path $installRoot 'OpenSquilla.exe'
  $gateways = @(Get-ChildItem -LiteralPath (Join-Path $installRoot 'resources/runtime/gateway') -Recurse -Filter opensquilla-gateway.exe -File)
  if ($gateways.Count -ne 1) { throw 'Expected exactly one frozen Gateway.' }
  $gateway = $gateways[0].FullName
  switch ($Probe) {
    'startup-compat' {
      python .github/scripts/verify-packaged-startup-compatibility.py --gateway $gateway --workdir (Join-Path $evidence 'gateway') --output (Join-Path $evidence 'gateway.json')
      if ($LASTEXITCODE -ne 0) { throw 'Frozen startup compatibility failed.' }
      node desktop/electron/scripts/test-packaged-startup-compatibility.mjs --executable $app --workdir (Join-Path $evidence 'electron') --output (Join-Path $evidence 'electron.json') --scenario compatibility
      if ($LASTEXITCODE -ne 0) { throw 'Installed Electron startup compatibility failed.' }
    }
    'ownership' {
      python .github/scripts/verify-packaged-ownership-long-paths.py --gateway $gateway --workdir (Join-Path $evidence 'gateway') --output (Join-Path $evidence 'gateway.json')
      if ($LASTEXITCODE -ne 0) { throw 'Frozen ownership long-path gate failed.' }
      node desktop/electron/scripts/test-packaged-startup-compatibility.mjs --executable $app --workdir (Join-Path $evidence 'electron') --output (Join-Path $evidence 'electron.json') --scenario long-path
      if ($LASTEXITCODE -ne 0) { throw 'Installed Electron ownership long-path gate failed.' }
    }
    'migration' {
      $profile = Join-Path $evidence 'complete-v054-profile'
      python .github/scripts/verify-release-profile-preservation.py seed --home $profile --label candidate-migration --baseline-version 0.5.4
      if ($LASTEXITCODE -ne 0) { throw 'Complete migration fixture seed failed.' }
      python .github/scripts/verify-packaged-v054-upgrade.py --gateway $gateway --home $profile --output (Join-Path $evidence 'migration.json')
      if ($LASTEXITCODE -ne 0) { throw 'Complete migration and restart gate failed.' }
    }
    'session' {
      $userData = Join-Path $evidence 'user-data'
      python .github/scripts/verify-release-profile-preservation.py seed --home (Join-Path $userData 'opensquilla') --label candidate-session --baseline-version 0.5.4
      if ($LASTEXITCODE -ne 0) { throw 'Session fixture seed failed.' }
      node desktop/electron/scripts/test-packaged-session-recovery.mjs --executable $app --user-data-dir $userData --label candidate-session --session-key agent:main:webchat:release-recovery-long-session --switch-session-key agent:main:webchat:release-recovery-switch-session --verify-recovered-send
      if ($LASTEXITCODE -ne 0) { throw 'Installed session recovery gate failed.' }
    }
  }
} finally {
  if ($installed) {
    $uninstallers = @(Get-ChildItem -LiteralPath $installRoot -Filter 'Uninstall*.exe' -File)
    if ($uninstallers.Count -ne 1) { throw 'Expected exactly one candidate uninstaller.' }
    $uninstall = Start-Process -FilePath $uninstallers[0].FullName -ArgumentList '/S' -WindowStyle Hidden -PassThru
    if (-not $uninstall.WaitForExit(120000)) { throw 'Candidate uninstall deadline exceeded.' }
    if ($uninstall.ExitCode -ne 0) { throw "Candidate uninstaller exit $($uninstall.ExitCode)." }
  }
}
