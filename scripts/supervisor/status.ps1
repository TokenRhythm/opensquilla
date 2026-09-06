<#
.SYNOPSIS
    Show one status row per OpenSquilla profile gateway.
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

$rows = @()
foreach ($entry in $entries) {
    $port = Get-ProfilePort -Name $entry.Name -BasePort $BasePort -ProfilesRoot $root
    $statusArgs = @('--profile', $entry.Name, 'gateway', 'status', '--listen', $BindHost, '--port', [string]$port, '--json')
    $result = Invoke-Opensquilla -Repo $Repo -Profile $entry.Path -Arguments $statusArgs -CaptureOutput
    $parsed = $null
    if ($result.Output) {
        $parsed = $result.Output | ConvertFrom-Json -ErrorAction SilentlyContinue
    }

    if ($parsed) {
        $rows += [pscustomobject]@{
            Profile = $entry.Name
            State = [string]$parsed.state
            Port = [int]$parsed.port
            Host = [string]$parsed.host
            Pid = if ($parsed.pid) { [int]$parsed.pid } else { '-' }
            Log = [string]$parsed.logPath
        }
    } else {
        $rows += [pscustomobject]@{
            Profile = $entry.Name
            State = 'unknown'
            Port = $port
            Host = '-'
            Pid = '-'
            Log = '-'
        }
    }
}

function Format-OpenSquillaTable {
    param([object[]] $Data)

    $cols = 'Profile', 'State', 'Port', 'Host', 'Pid', 'Log'
    $widths = @{}
    foreach ($col in $cols) {
        $widths[$col] = $col.Length
    }
    foreach ($row in $Data) {
        foreach ($col in $cols) {
            $value = [string]$row.$col
            if ($value.Length -gt $widths[$col]) {
                $widths[$col] = $value.Length
            }
        }
    }

    $header = ($cols | ForEach-Object { $_.PadRight($widths[$_]) }) -join '  '
    Write-Host $header -ForegroundColor Cyan
    Write-Host ('-' * $header.Length) -ForegroundColor DarkGray
    foreach ($row in $Data) {
        $line = ($cols | ForEach-Object { ([string]$row.$_).PadRight($widths[$_]) }) -join '  '
        $color = switch ($row.State) {
            'running' { 'Green' }
            'unhealthy' { 'Red' }
            'not_started' { 'DarkGray' }
            default { 'Yellow' }
        }
        Write-Host $line -ForegroundColor $color
    }
}

Format-OpenSquillaTable -Data $rows

$misconfigured = $rows | Where-Object { $_.State -notin @('running', 'not_started') }
if ($misconfigured.Count -gt 0) {
    Write-Status ("{0} profile(s) need attention" -f $misconfigured.Count) -Level warn
    exit 1
}
