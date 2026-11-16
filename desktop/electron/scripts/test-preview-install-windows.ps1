param(
  [Parameter(Mandatory = $true)][string]$PackageRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# This probe deliberately creates synthetic stable-install records. Never run it
# on a workstation, including a self-hosted runner with an everyday user profile.
if ($env:CI -ine 'true' -or $env:OS -ne 'Windows_NT' -or
    [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP) -or
    -not (Test-Path -LiteralPath $env:RUNNER_TEMP -PathType Container) -or
    $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
  throw 'Preview installation verification requires a disposable GitHub-hosted Windows CI runner.'
}

$evidencePath = Join-Path $env:RUNNER_TEMP 'opensquilla-preview-install-evidence.json'
$stableGuid = '9f633106-fcda-5e7f-93e4-8dddb967ea3e'
$previewGuid = '277ac0ed-967c-587e-920a-ccf5ab1172f6'
$uninstallPrefix = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\'
$stableInstallDirectory = Join-Path $env:LOCALAPPDATA 'Programs\OpenSquilla'
$expectedPreviewInstallDirectory = Join-Path $env:LOCALAPPDATA 'Programs\OpenSquilla Preview'
$previewUserData = Join-Path $env:APPDATA 'opensquilla-preview'
$stableDirectories = @(
  $stableInstallDirectory,
  (Join-Path $env:APPDATA '@opensquilla\desktop-electron'),
  (Join-Path $env:APPDATA 'OpenSquilla')
)
$stableRegistryPaths = @(
  "Software\$stableGuid",
  "$uninstallPrefix$stableGuid",
  'Software\Classes\opensquilla'
)
$previewRegistryPaths = @(
  "Software\$previewGuid",
  "$uninstallPrefix$previewGuid",
  'Software\Classes\opensquilla-preview'
)
$checks = [System.Collections.Generic.List[object]]::new()
$evidence = [ordered]@{
  schemaVersion = 1
  startedAt = [DateTime]::UtcNow.ToString('o')
  guardPassed = $true
  passed = $false
  checks = $checks
  preview = [ordered]@{ guid = $previewGuid; userData = $previewUserData }
  cleanup = [ordered]@{ rootProcessIds = @(); stoppedProcessIds = @(); errors = @() }
}
$previewExe = $null
$baseline = $null
$failure = $null

function Add-Check([string]$Stage, [bool]$Passed, [string]$Detail) {
  $checks.Add([ordered]@{
    stage = $Stage; passed = $Passed; detail = $Detail
    at = [DateTime]::UtcNow.ToString('o')
  })
}

function Get-TextDigest([string]$Text) {
  $hash = [System.Security.Cryptography.SHA256]::Create()
  try {
    return ([BitConverter]::ToString($hash.ComputeHash(
      [System.Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant()
  } finally { $hash.Dispose() }
}

function Get-RegistrySnapshot([Microsoft.Win32.RegistryHive]$Hive, [string]$Path) {
  $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, [Microsoft.Win32.RegistryView]::Registry64)
  try {
    $key = $base.OpenSubKey($Path, $false)
    if ($null -eq $key) { return 'absent' }
    $nodes = [System.Collections.Generic.List[object]]::new()
    function Read-RegistryNode($Node, [string]$Relative) {
      $values = @(foreach ($name in @($Node.GetValueNames() | Sort-Object)) {
        [ordered]@{
          name = $name
          kind = $Node.GetValueKind($name).ToString()
          value = $Node.GetValue($name, $null,
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        }
      })
      $nodes.Add([ordered]@{ relative = $Relative; values = $values })
      foreach ($name in @($Node.GetSubKeyNames() | Sort-Object)) {
        $child = $Node.OpenSubKey($name, $false)
        try {
          $next = if ($Relative) { "$Relative\$name" } else { $name }
          Read-RegistryNode $child $next
        } finally { $child.Dispose() }
      }
    }
    try { Read-RegistryNode $key '' } finally { $key.Dispose() }
    return ($nodes.ToArray() | ConvertTo-Json -Depth 20 -Compress)
  } finally { $base.Dispose() }
}

function Get-DirectorySnapshot([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return 'absent' }
  if (((Get-Item -LiteralPath $Path).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'A synthetic stable directory was replaced with a redirected path.'
  }
  $rows = @(foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Recurse -Force | Sort-Object FullName)) {
    if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'A synthetic stable directory was replaced with a redirected path.'
    }
    [ordered]@{
      relative = $entry.FullName.Substring($Path.Length)
      directory = $entry.PSIsContainer
      sha256 = if ($entry.PSIsContainer) { $null } else {
        (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
      }
    }
  })
  return ($rows | ConvertTo-Json -Depth 8 -Compress)
}

function Assert-StableUnchanged([string]$Stage) {
  foreach ($path in $stableDirectories) {
    if ((Get-DirectorySnapshot $path) -cne $baseline.directories[$path]) {
      throw "Stable directory changed during $Stage."
    }
  }
  foreach ($path in $stableRegistryPaths) {
    if ((Get-RegistrySnapshot CurrentUser $path) -cne $baseline.registry[$path]) {
      throw "Stable registry record changed during $Stage."
    }
  }
  Add-Check $Stage $true 'Synthetic stable installation, profiles and protocol remain unchanged.'
}

function Set-SyntheticRegistry([string]$Path, [hashtable]$Values) {
  $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($Path, $true)
  try {
    foreach ($name in $Values.Keys) {
      $key.SetValue([string]$name, [string]$Values[$name], [Microsoft.Win32.RegistryValueKind]::String)
    }
  } finally { $key.Dispose() }
}

function Get-PreviewInstallLocation {
  $locations = [System.Collections.Generic.List[string]]::new()
  foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, [Microsoft.Win32.RegistryView]::Registry64)
    try {
      $uninstall = $base.OpenSubKey("$uninstallPrefix$previewGuid", $false)
      if ($null -eq $uninstall) { continue }
      try {
        if ([string]$uninstall.GetValue('DisplayName', '') -notlike 'OpenSquilla Preview*') {
          throw 'Preview uninstall GUID has an unexpected product identity.'
        }
      } finally { $uninstall.Dispose() }
      $install = $base.OpenSubKey("Software\$previewGuid", $false)
      if ($null -eq $install) { throw 'Preview installation location registry record is missing.' }
      try { $location = [string]$install.GetValue('InstallLocation', '') } finally { $install.Dispose() }
      if ([string]::IsNullOrWhiteSpace($location) -or -not [System.IO.Path]::IsPathRooted($location)) {
        throw 'Preview installation location is invalid.'
      }
      $locations.Add([System.IO.Path]::GetFullPath($location).TrimEnd('\'))
    } finally { $base.Dispose() }
  }
  if ($locations.Count -ne 1) { throw 'Expected exactly one Preview uninstall registration.' }
  return $locations[0]
}

function Get-PreviewProcesses {
  if (-not $previewExe) { return @() }
  return @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and [string]::Equals(
      [System.IO.Path]::GetFullPath($_.ExecutablePath), $previewExe,
      [System.StringComparison]::OrdinalIgnoreCase)
  })
}

function Stop-PreviewProcessTrees {
  if (-not $previewExe) { return }
  $snapshot = @(Get-CimInstance Win32_Process)
  $roots = @(Get-PreviewProcesses)
  $evidence.cleanup.rootProcessIds = @($roots | ForEach-Object { [int]$_.ProcessId })
  $owned = @{}
  foreach ($root in $roots) { $owned[[int]$root.ProcessId] = $root }
  do {
    $added = $false
    foreach ($process in $snapshot) {
      $processId = [int]$process.ProcessId
      $parentId = [int]$process.ParentProcessId
      if (-not $owned.ContainsKey($processId) -and $owned.ContainsKey($parentId) -and
          $process.CreationDate -ge $owned[$parentId].CreationDate) {
        $owned[$processId] = $process
        $added = $true
      }
    }
  } while ($added)
  $stopped = [System.Collections.Generic.List[int]]::new()
  foreach ($process in @($owned.Values | Sort-Object CreationDate -Descending)) {
    $processId = [int]$process.ProcessId
    $live = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
    if ($null -eq $live -or $live.CreationDate -ne $process.CreationDate -or
        $live.ExecutablePath -ne $process.ExecutablePath) { continue }
    try { Stop-Process -Id $processId -Force -ErrorAction Stop } catch {
      $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
      if ($null -ne $remaining -and $remaining.CreationDate -eq $process.CreationDate) { throw }
    }
    $stopped.Add($processId)
  }
  $evidence.cleanup.stoppedProcessIds = @($stopped.ToArray())
  if (@(Get-PreviewProcesses).Count -gt 0) { throw 'An installed Preview process survived cleanup.' }
}

try {
  if (-not (Test-Path -LiteralPath $PackageRoot -PathType Container)) { throw 'PackageRoot does not exist.' }
  $installers = @(Get-ChildItem -LiteralPath $PackageRoot -Filter 'OpenSquilla-Preview-*-win-x64.exe' -File)
  if ($installers.Count -ne 1) { throw 'Expected exactly one OpenSquilla Preview Windows installer.' }
  $installer = $installers[0]
  $evidence.installer = [ordered]@{
    name = $installer.Name
    sha256 = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  }
  foreach ($path in @($stableDirectories) + @($previewUserData, $expectedPreviewInstallDirectory)) {
    if (Test-Path -LiteralPath $path) { throw 'Refuse to overwrite an existing application or profile directory.' }
  }
  foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
    foreach ($path in @($stableRegistryPaths) + @($previewRegistryPaths)) {
      if ((Get-RegistrySnapshot $hive $path) -ne 'absent') {
        throw 'Refuse to overwrite an existing installation or protocol registry record.'
      }
    }
  }
  Add-Check 'preflight' $true 'Disposable runner has no existing stable or Preview records at the test boundaries.'

  foreach ($path in $stableDirectories) {
    $null = New-Item -ItemType Directory -Path $path
    [System.IO.File]::WriteAllText((Join-Path $path 'synthetic-stable-sentinel.txt'),
      'Synthetic stable OpenSquilla sentinel. Preserve this file and directory.', [System.Text.UTF8Encoding]::new($false))
  }
  Set-SyntheticRegistry "Software\$stableGuid" @{
    SyntheticSentinel = 'preserve-stable-install'; InstallLocation = $stableInstallDirectory
  }
  Set-SyntheticRegistry "$uninstallPrefix$stableGuid" @{
    SyntheticSentinel = 'preserve-stable-uninstall'; DisplayName = 'Synthetic stable OpenSquilla'
  }
  Set-SyntheticRegistry 'Software\Classes\opensquilla' @{
    '' = 'URL:Synthetic stable OpenSquilla'; 'URL Protocol' = ''; SyntheticSentinel = 'preserve-stable-protocol'
  }
  Set-SyntheticRegistry 'Software\Classes\opensquilla\shell\open\command' @{
    '' = ('"' + (Join-Path $stableInstallDirectory 'OpenSquilla.exe') + '" "%1"')
  }
  $baseline = @{ directories = @{}; registry = @{} }
  foreach ($path in $stableDirectories) { $baseline.directories[$path] = Get-DirectorySnapshot $path }
  foreach ($path in $stableRegistryPaths) { $baseline.registry[$path] = Get-RegistrySnapshot CurrentUser $path }
  $evidence.stableSentinels = [ordered]@{
    directories = @($stableDirectories | ForEach-Object {
      [ordered]@{ path = $_; sha256 = Get-TextDigest $baseline.directories[$_] }
    })
    registry = @($stableRegistryPaths | ForEach-Object {
      [ordered]@{ path = $_; sha256 = Get-TextDigest $baseline.registry[$_] }
    })
  }

  $install = Start-Process -FilePath $installer.FullName -ArgumentList @('/S', '/currentuser') -PassThru
  if (-not $install.WaitForExit(180000)) { throw 'Silent Preview installation exceeded three minutes.' }
  if ($install.ExitCode -ne 0) { throw "Silent Preview installation failed with exit code $($install.ExitCode)." }
  Add-Check 'install' $true 'Preview installer completed successfully.'
  $installLocation = Get-PreviewInstallLocation
  $previewExe = Join-Path $installLocation 'OpenSquilla Preview.exe'
  Assert-StableUnchanged 'after_install'
  foreach ($path in $stableDirectories) {
    $protected = [System.IO.Path]::GetFullPath($path).TrimEnd('\')
    if ($installLocation.Equals($protected, [System.StringComparison]::OrdinalIgnoreCase) -or
        $installLocation.StartsWith($protected + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'Preview installation overlaps a synthetic stable directory.'
    }
  }
  if (-not (Test-Path -LiteralPath $previewExe -PathType Leaf)) { throw 'Installed Preview executable is missing.' }
  $evidence.preview.installLocation = $installLocation
  $evidence.preview.executable = $previewExe

  # No flags or environment overrides: exercise the ordinary installed launch.
  $null = Start-Process -FilePath $previewExe -PassThru
  $deadline = [DateTime]::UtcNow.AddSeconds(45)
  $readySamples = 0
  do {
    $mainProcesses = @(Get-PreviewProcesses | Where-Object { $_.CommandLine -notmatch '--type=' })
    if ($mainProcesses.Count -gt 0 -and (Test-Path -LiteralPath $previewUserData -PathType Container)) {
      $readySamples += 1
    } else { $readySamples = 0 }
    if ($readySamples -ge 3) { break }
    Start-Sleep -Milliseconds 1000
  } while ([DateTime]::UtcNow -lt $deadline)
  if ($readySamples -lt 3) { throw 'Preview did not retain a live main process and its independent profile within 45 seconds.' }
  if (((Get-Item -LiteralPath $previewUserData).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Preview profile must be an independent directory, not a redirected path.'
  }
  $evidence.preview.mainProcessIds = @($mainProcesses | ForEach-Object { [int]$_.ProcessId })
  $evidence.preview.profileCreated = $true
  Add-Check 'normal_launch' $true 'Installed Preview remains alive with its independent default profile.'
  Assert-StableUnchanged 'after_launch'

  $protocol = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Classes\opensquilla-preview', $false)
  if ($null -eq $protocol) { throw 'Preview URL protocol registration is missing.' }
  try {
    if ($protocol.GetValueNames() -notcontains 'URL Protocol') { throw 'Preview URL protocol marker is missing.' }
    $command = $protocol.OpenSubKey('shell\open\command', $false)
    if ($null -eq $command) { throw 'Preview URL protocol command is missing.' }
    try { $commandText = [string]$command.GetValue('', '') } finally { $command.Dispose() }
  } finally { $protocol.Dispose() }
  if ($commandText -notmatch '^\s*"([^"]+)"(?:\s|$)' -or
      -not [string]::Equals([System.IO.Path]::GetFullPath($Matches[1]), $previewExe,
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Preview URL protocol does not target the installed Preview executable.'
  }
  Add-Check 'protocol' $true 'opensquilla-preview targets Preview; the stable scheme is unchanged.'
  $evidence.passed = $true
} catch {
  $failure = $_.Exception.Message
  Add-Check 'failure' $false $failure
} finally {
  if (-not $previewExe -and $null -ne $baseline) {
    try { $previewExe = Join-Path (Get-PreviewInstallLocation) 'OpenSquilla Preview.exe' } catch {
      # A failed installer may not have registered any Preview executable.
    }
  }
  try { Stop-PreviewProcessTrees } catch {
    $evidence.cleanup.errors = @($_.Exception.Message)
    $evidence.passed = $false
    if (-not $failure) { $failure = 'Preview process cleanup failed.' }
  }
  if ($null -ne $baseline) {
    try { Assert-StableUnchanged 'after_cleanup' } catch {
      $evidence.passed = $false
      if (-not $failure) { $failure = $_.Exception.Message }
      Add-Check 'after_cleanup' $false $_.Exception.Message
    }
  }
  if ($failure) { $evidence.error = $failure }
  $evidence.completedAt = [DateTime]::UtcNow.ToString('o')
  [System.IO.File]::WriteAllText($evidencePath, ($evidence | ConvertTo-Json -Depth 20),
    [System.Text.UTF8Encoding]::new($false))
}
if (-not $evidence.passed) { throw "Preview installation verification failed: $failure. Evidence: $evidencePath" }
Write-Output "Preview installation verification passed. Evidence: $evidencePath"
exit 0
