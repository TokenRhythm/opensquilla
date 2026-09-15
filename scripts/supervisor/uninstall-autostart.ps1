<#
.SYNOPSIS
    Remove the OpenSquilla multi-profile supervisor from Task Scheduler.
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'OpenSquillaProfileSupervisor'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')

if (-not (Get-Command schtasks.exe -ErrorAction SilentlyContinue)) {
    throw 'schtasks.exe not found. This script only runs on Windows.'
}

$query = Start-Process -FilePath schtasks.exe `
    -ArgumentList @('/Query', '/TN', $TaskName) `
    -NoNewWindow -Wait -PassThru
if ($query.ExitCode -ne 0) {
    Write-Status "Task '$TaskName' is not registered; nothing to do." -Level warn
    return
}

$delete = Start-Process -FilePath schtasks.exe `
    -ArgumentList @('/Delete', '/TN', $TaskName, '/F') `
    -NoNewWindow -Wait -PassThru
if ($delete.ExitCode -ne 0) {
    throw "schtasks /Delete exited with code $($delete.ExitCode)"
}
Write-Status "Removed task '$TaskName'." -Level ok
