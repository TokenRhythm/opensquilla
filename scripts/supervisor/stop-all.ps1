<#
.SYNOPSIS
    Stop every OpenSquilla profile gateway under a Windows profiles root.
#>
[CmdletBinding()]
param(
    [string] $ProfilesRoot,
    [int] $BasePort = 18791,
    [string] $BindHost = '127.0.0.1',
    [string] $Repo
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')

$root = Get-ProfilesRoot -Override $ProfilesRoot
$entries = Get-ProfileEntries -ProfilesRoot $root

if (-not $entries -or $entries.Count -eq 0) {
    Write-Status "No profiles found under $root." -Level warn
    return
}

$stopped = 0
$skipped = 0
$failed = 0

foreach ($entry in $entries) {
    $port = Get-ProfilePort -Name $entry.Name -BasePort $BasePort -ProfilesRoot $root
    Write-Status ("[{0}] stopping on port {1} ..." -f $entry.Name, $port)
    try {
        $stopArgs = @('--profile', $entry.Name, 'gateway', 'stop', '--listen', $BindHost, '--port', [string]$port)
        $result = Invoke-Opensquilla -Repo $Repo -Profile $entry.Path -Arguments $stopArgs
        if ($result.ExitCode -eq 0) {
            Write-Status ("[{0}] stopped" -f $entry.Name) -Level ok
            $stopped += 1
        } else {
            Write-Status ("[{0}] not running (exit={1})" -f $entry.Name, $result.ExitCode) -Level ok
            $skipped += 1
        }
    } catch {
        Write-Status ("[{0}] threw: {1}" -f $entry.Name, $_.Exception.Message) -Level err
        $failed += 1
    }
}

Write-Host ''
$summaryLevel = if ($failed -eq 0) { 'ok' } else { 'warn' }
Write-Status ("Summary: stopped={0} skipped={1} failed={2}" -f $stopped, $skipped, $failed) -Level $summaryLevel

if ($failed -gt 0) {
    exit 1
}
