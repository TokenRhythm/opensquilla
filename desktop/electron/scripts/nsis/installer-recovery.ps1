param(
  [Parameter(Mandatory = $true)][int]$ParentPid,
  [Parameter(Mandatory = $true)][string]$InstallRoot,
  [Parameter(Mandatory = $true)][string]$RecoveryRoot,
  [Parameter(Mandatory = $true)][string]$RegistryRoot,
  [string]$SecondaryRegistryRoot = '',
  [Parameter(Mandatory = $true)][string]$InstallRegistryKey,
  [Parameter(Mandatory = $true)][string]$UninstallRegistryKey,
  [string]$UninstallRegistryKey2 = '',
  [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
$phasePath = Join-Path $RecoveryRoot 'phase.txt'
$readyPath = Join-Path $RecoveryRoot 'ready'
$commitPath = Join-Path $RecoveryRoot 'commit'
$errorPath = Join-Path $RecoveryRoot 'error.txt'
$restoredPath = Join-Path $RecoveryRoot 'restored'
$registryInstallPath = Join-Path $RecoveryRoot 'install.reg'
$registryUninstallPath = Join-Path $RecoveryRoot 'uninstall.reg'
$registryUninstall2Path = Join-Path $RecoveryRoot 'uninstall-2.reg'
$registryInstallSecondaryPath = Join-Path $RecoveryRoot 'install-secondary.reg'
$registryUninstallSecondaryPath = Join-Path $RecoveryRoot 'uninstall-secondary.reg'
$registryUninstall2SecondaryPath = Join-Path $RecoveryRoot 'uninstall-2-secondary.reg'
$oldInstallRoot = Join-Path $RecoveryRoot 'old-install'

function Write-Marker([string]$Path, [string]$Value = '') {
  Set-Content -LiteralPath $Path -Value $Value -Encoding ascii -Force
}

function Invoke-Reg([string[]]$Arguments) {
  $process = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\reg.exe') `
    -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
  if ($process.ExitCode -ne 0) { throw "reg.exe failed ($($process.ExitCode)): $($Arguments -join ' ')" }
}

function Export-RegistryKey([string]$Key, [string]$Destination) {
  if (-not $Key) { return $false }
  & reg.exe query $Key 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { return $false }
  Invoke-Reg @('export', $Key, $Destination, '/y')
  return $true
}

function Restore-RegistryKey([string]$Key, [string]$Backup, [bool]$WasPresent) {
  if (-not $Key) { return }
  & reg.exe delete $Key /f 2>$null | Out-Null
  if ($WasPresent -and (Test-Path -LiteralPath $Backup -PathType Leaf)) {
    Invoke-Reg @('import', $Backup)
  }
}

function Wait-UntilAvailable([string]$Path, [int]$Attempts = 120) {
  for ($i = 0; $i -lt $Attempts; $i++) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Start-Sleep -Milliseconds 500
  }
  throw "Path remained locked: $Path"
}

function Stop-ProcessesUnderRoot([string]$Root) {
  $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
  Get-Process -Name 'OpenSquilla', 'opensquilla-gateway' -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $path = $_.Path
      if ($path -and [IO.Path]::GetFullPath($path).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
      }
    } catch { }
  }
}

function Wait-ForParentExit([int]$ProcessId) {
  while ($true) {
    try {
      $process = Get-Process -Id $ProcessId -ErrorAction Stop
      if ($process.HasExited) { return }
    } catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
      return
    } catch {
      if ($_.Exception.Message -match 'cannot find|not found|no process') { return }
      throw
    }
    Start-Sleep -Milliseconds 250
  }
}

try {
  New-Item -ItemType Directory -Path $RecoveryRoot -Force | Out-Null
  if (Test-Path -LiteralPath $oldInstallRoot) { Remove-Item -LiteralPath $oldInstallRoot -Recurse -Force }
  $oldExists = Test-Path -LiteralPath $InstallRoot -PathType Container
  if ($oldExists) {
    $robocopy = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\robocopy.exe') `
      -ArgumentList @($InstallRoot, $oldInstallRoot, '/E', '/COPY:DAT', '/DCOPY:DAT', '/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS') `
      -Wait -PassThru -WindowStyle Hidden
    if ($robocopy.ExitCode -gt 7) { throw "robocopy failed with exit code $($robocopy.ExitCode)" }
  }
  $installPresent = Export-RegistryKey "$RegistryRoot\$InstallRegistryKey" $registryInstallPath
  $uninstallPresent = Export-RegistryKey "$RegistryRoot\$UninstallRegistryKey" $registryUninstallPath
  $uninstall2Present = if ($UninstallRegistryKey2) {
    Export-RegistryKey "$RegistryRoot\$UninstallRegistryKey2" $registryUninstall2Path
  } else { $false }
  $installSecondaryPresent = $false
  $uninstallSecondaryPresent = $false
  $uninstall2SecondaryPresent = $false
  if ($SecondaryRegistryRoot) {
    $installSecondaryPresent = Export-RegistryKey "$SecondaryRegistryRoot\$InstallRegistryKey" $registryInstallSecondaryPath
    $uninstallSecondaryPresent = Export-RegistryKey "$SecondaryRegistryRoot\$UninstallRegistryKey" $registryUninstallSecondaryPath
    if ($UninstallRegistryKey2) {
      $uninstall2SecondaryPresent = Export-RegistryKey "$SecondaryRegistryRoot\$UninstallRegistryKey2" $registryUninstall2SecondaryPath
    }
  }
  Write-Marker (Join-Path $RecoveryRoot 'presence.json') (@{
    install = $installPresent; uninstall = $uninstallPresent; uninstall2 = $uninstall2Present
    installSecondary = $installSecondaryPresent; uninstallSecondary = $uninstallSecondaryPresent
    uninstall2Secondary = $uninstall2SecondaryPresent; oldInstall = $oldExists
  } | ConvertTo-Json -Compress)
  Write-Marker $phasePath 'ready'
  Write-Marker $readyPath
  if ($PrepareOnly) { exit 0 }

  Wait-ForParentExit $ParentPid
  if (Test-Path -LiteralPath $commitPath -PathType Leaf) {
    Remove-Item -LiteralPath $RecoveryRoot -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
  }

  Write-Marker $phasePath 'restoring'
  Stop-ProcessesUnderRoot $InstallRoot
  if (Test-Path -LiteralPath $InstallRoot) {
    $failedRoot = Join-Path $RecoveryRoot 'failed-install'
    if (Test-Path -LiteralPath $failedRoot) { Remove-Item -LiteralPath $failedRoot -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item -LiteralPath $InstallRoot -Destination $failedRoot -Force
  }
  if (-not (Test-Path -LiteralPath $oldInstallRoot -PathType Container)) {
    throw 'The saved installation directory is missing.'
  }
  New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
  Copy-Item -Path (Join-Path $oldInstallRoot '*') -Destination $InstallRoot -Recurse -Force
  $presence = Get-Content -LiteralPath (Join-Path $RecoveryRoot 'presence.json') -Raw | ConvertFrom-Json
  Restore-RegistryKey "$RegistryRoot\$InstallRegistryKey" $registryInstallPath ([bool]$presence.install)
  Restore-RegistryKey "$RegistryRoot\$UninstallRegistryKey" $registryUninstallPath ([bool]$presence.uninstall)
  if ($UninstallRegistryKey2) {
    Restore-RegistryKey "$RegistryRoot\$UninstallRegistryKey2" $registryUninstall2Path ([bool]$presence.uninstall2)
  }
  if ($SecondaryRegistryRoot) {
    Restore-RegistryKey "$SecondaryRegistryRoot\$InstallRegistryKey" $registryInstallSecondaryPath ([bool]$presence.installSecondary)
    Restore-RegistryKey "$SecondaryRegistryRoot\$UninstallRegistryKey" $registryUninstallSecondaryPath ([bool]$presence.uninstallSecondary)
    if ($UninstallRegistryKey2) {
      Restore-RegistryKey "$SecondaryRegistryRoot\$UninstallRegistryKey2" $registryUninstall2SecondaryPath ([bool]$presence.uninstall2Secondary)
    }
  }
  Write-Marker $phasePath 'restored'
  Write-Marker $restoredPath
} catch {
  try { Write-Marker $errorPath $_.Exception.ToString() } catch { }
  exit 1
}
