<#
.SYNOPSIS
    Register start-all.ps1 with Windows Task Scheduler at user logon.
#>
[CmdletBinding()]
param(
    [string] $ProfilesRoot,
    [int] $BasePort = 18791,
    [string] $TaskName = 'OpenSquillaProfileSupervisor',
    [string] $Repo
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')

if (-not (Get-Command schtasks.exe -ErrorAction SilentlyContinue)) {
    throw 'schtasks.exe not found. This script only runs on Windows.'
}

$root = Get-ProfilesRoot -Override $ProfilesRoot
$startAll = Join-Path $PSScriptRoot 'start-all.ps1'

$commandParts = @(
    '&',
    (ConvertTo-PowerShellSingleQuotedLiteral $startAll),
    '-ProfilesRoot',
    (ConvertTo-PowerShellSingleQuotedLiteral $root),
    '-BasePort',
    [string]$BasePort,
    '-SkipRunning'
)
if ($Repo) {
    $commandParts += @('-Repo', (ConvertTo-PowerShellSingleQuotedLiteral $Repo))
}
$command = $commandParts -join ' '

$encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
$arguments = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
$escapedRoot = ConvertTo-XmlEscapedText $root
$escapedUser = ConvertTo-XmlEscapedText "$env:USERDOMAIN\$env:USERNAME"
$escapedAuthor = ConvertTo-XmlEscapedText $env:USERNAME
$escapedArguments = ConvertTo-XmlEscapedText $arguments

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$escapedAuthor</Author>
    <Description>Auto-start every OpenSquilla profile gateway under $escapedRoot at user logon.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escapedUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$escapedArguments</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$safeTaskName = ($TaskName -replace '[^a-zA-Z0-9_.-]', '_')
$xmlPath = Join-Path $env:TEMP "opensquilla-supervisor-$safeTaskName.xml"
[System.IO.File]::WriteAllText($xmlPath, $taskXml, [System.Text.Encoding]::Unicode)

try {
    $proc = Start-Process -FilePath schtasks.exe `
        -ArgumentList @('/Create', '/TN', $TaskName, '/XML', $xmlPath, '/F') `
        -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "schtasks /Create exited with code $($proc.ExitCode)"
    }
    Write-Status "Registered task '$TaskName' to run start-all.ps1 at logon." -Level ok
    Write-Status "Profiles root: $root" -Level info
    Write-Status "Base port: $BasePort" -Level info
} finally {
    Remove-Item -LiteralPath $xmlPath -ErrorAction SilentlyContinue
}
