<#
.SYNOPSIS
    Start every OpenSquilla profile gateway under a Windows profiles root.
#>
[CmdletBinding()]
param(
    [string] $ProfilesRoot,
    [int] $BasePort = 18791,
    [string] $BindHost = '127.0.0.1',
    [switch] $SkipRunning,
    [string] $Repo
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')

$root = Get-ProfilesRoot -Override $ProfilesRoot
$cmd = Get-OpensquillaCommand -Repo $Repo
$entries = Get-ProfileEntries -ProfilesRoot $root

if (-not $entries -or $entries.Count -eq 0) {
    Write-Status "No profiles found under $root. Run opensquilla --profile <name> init first." -Level warn
    return
}

Write-Status "Discovered $($entries.Count) profile(s) under $root" -Level info
Write-Status "Base port: $BasePort" -Level info
switch ($cmd.Mode) {
    'installed' { Write-Status "Mode: installed opensquilla at $($cmd.Exe)" -Level info }
    'uv-run-repo' { Write-Status "Mode: uv run from $($cmd.Repo)" -Level info }
    default { Write-Status 'Mode: no opensquilla found; set PATH or pass -Repo' -Level err }
}
Write-Host ''

$started = 0
$skipped = 0
$failed = 0

foreach ($entry in $entries) {
    $port = Get-ProfilePort -Name $entry.Name -BasePort $BasePort -ProfilesRoot $root
    Write-Status ("[{0}] starting on port {1} ..." -f $entry.Name, $port)
    try {
        if ($SkipRunning) {
            $statusArgs = @('--profile', $entry.Name, 'gateway', 'status', '--listen', $BindHost, '--port', [string]$port, '--json')
            $status = Invoke-Opensquilla -Repo $Repo -Profile $entry.Path -Arguments $statusArgs -CaptureOutput
            $parsed = $null
            if ($status.Output) {
                $parsed = $status.Output | ConvertFrom-Json -ErrorAction SilentlyContinue
            }
            if ($parsed -and [string]$parsed.state -eq 'running') {
                Write-Status ("[{0}] already running on port {1}; skipped" -f $entry.Name, $port) -Level ok
                $skipped += 1
                continue
            }
        }

        $startArgs = @('--profile', $entry.Name, 'gateway', 'start', '--listen', $BindHost, '--port', [string]$port)
        $result = Invoke-Opensquilla -Repo $Repo -Profile $entry.Path -Arguments $startArgs
        if ($result.ExitCode -eq 0) {
            Write-Status ("[{0}] up on port {1}" -f $entry.Name, $port) -Level ok
            $started += 1
        } else {
            Write-Status ("[{0}] start failed (exit={1})" -f $entry.Name, $result.ExitCode) -Level err
            $failed += 1
        }
    } catch {
        Write-Status ("[{0}] threw: {1}" -f $entry.Name, $_.Exception.Message) -Level err
        $failed += 1
    }
}

Write-Host ''
$summaryLevel = if ($failed -eq 0) { 'ok' } else { 'warn' }
Write-Status ("Summary: started={0} skipped={1} failed={2}" -f $started, $skipped, $failed) -Level $summaryLevel

if ($failed -gt 0) {
    exit 1
}
