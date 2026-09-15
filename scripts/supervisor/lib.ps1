<#
.SYNOPSIS
    Shared helpers for OpenSquilla Windows multi-profile supervisor scripts.

.DESCRIPTION
    These helpers keep the Windows Task Scheduler wrapper thin: discover
    profile directories, allocate stable ports, resolve how to invoke
    OpenSquilla, and run a CLI command under one selected profile.
#>

$ErrorActionPreference = 'Stop'

if (Get-Variable -Name OPENSQUILLA_SUPERVISOR_LIB_LOADED -Scope Script -ErrorAction SilentlyContinue) {
    return
}
$Script:OPENSQUILLA_SUPERVISOR_LIB_LOADED = $true

$Script:DEFAULT_BASE_PORT = 18791
$Script:TASK_NAME = 'OpenSquillaProfileSupervisor'
$Script:DISPLAY_NAME = 'OpenSquilla Multi-Profile Gateway Supervisor'
$Script:PROFILE_NAME_PATTERN = '^[a-z0-9][a-z0-9_-]{0,63}$'
$Script:PORT_FILE_NAME = 'supervisor-port.txt'

function Get-DefaultProfilesRoot {
    $userProfile = [Environment]::GetFolderPath('UserProfile')
    if (-not $userProfile) {
        $userProfile = $HOME
    }
    return (Join-Path $userProfile '.opensquilla\profiles')
}

function Get-ProfilesRoot {
    param([string]$Override)

    $candidate = if ($Override) {
        $Override
    } elseif ($env:OPENSQUILLA_HOME) {
        $env:OPENSQUILLA_HOME
    } else {
        Get-DefaultProfilesRoot
    }

    if (-not $candidate) {
        throw 'Profiles root is empty. Pass -ProfilesRoot or set OPENSQUILLA_HOME.'
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "Profiles root does not exist: $candidate"
    }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Test-ProfileName {
    param([Parameter(Mandatory)] [string] $Name)
    return [bool]($Name -cmatch $Script:PROFILE_NAME_PATTERN)
}

function Get-OpensquillaRoot {
    param([string]$Override)

    if ($Override) {
        if (-not (Test-Path -LiteralPath $Override -PathType Container)) {
            throw "OpenSquilla repo not found: $Override. Pass -Repo or omit to auto-detect."
        }
        return (Resolve-Path -LiteralPath $Override).Path
    }

    if ($PSScriptRoot) {
        $candidate = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        if (Test-Path -LiteralPath (Join-Path $candidate 'pyproject.toml') -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    return $null
}

function Get-OpensquillaCommand {
    param([string]$Repo)

    if ($Repo) {
        $resolvedRepo = Get-OpensquillaRoot -Override $Repo
        if ($resolvedRepo -and (Test-Path -LiteralPath (Join-Path $resolvedRepo 'pyproject.toml') -PathType Leaf)) {
            return @{ Mode = 'uv-run-repo'; Repo = $resolvedRepo }
        }
        throw "OpenSquilla repo lacks pyproject.toml: $resolvedRepo"
    }

    $installed = Get-Command 'opensquilla' -ErrorAction SilentlyContinue
    if ($installed) {
        return @{ Mode = 'installed'; Exe = $installed.Path }
    }

    $resolvedRepo = Get-OpensquillaRoot -Override $null
    if ($resolvedRepo -and (Test-Path -LiteralPath (Join-Path $resolvedRepo 'pyproject.toml') -PathType Leaf)) {
        return @{ Mode = 'uv-run-repo'; Repo = $resolvedRepo }
    }

    return @{ Mode = 'none' }
}

function Get-ProfileEntries {
    param([Parameter(Mandatory)] [string] $ProfilesRoot)

    if (-not (Test-Path -LiteralPath $ProfilesRoot -PathType Container)) {
        return @()
    }

    $entries = Get-ChildItem -LiteralPath $ProfilesRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-ProfileName -Name $_.Name } |
        Sort-Object Name

    $results = @()
    foreach ($entry in $entries) {
        $configPath = Join-Path $entry.FullName 'config.toml'
        $results += [pscustomobject]@{
            Name = $entry.Name
            Path = $entry.FullName
            ConfigPath = $configPath
            HasConfig = Test-Path -LiteralPath $configPath -PathType Leaf
        }
    }
    return ,$results
}

function Get-ProfilePort {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [int] $BasePort,
        [Parameter(Mandatory)] [string] $ProfilesRoot
    )

    if (-not (Test-ProfileName -Name $Name)) {
        throw "Invalid OpenSquilla profile name: $Name"
    }

    $profilePath = Join-Path $ProfilesRoot $Name
    if (-not (Test-Path -LiteralPath $profilePath -PathType Container)) {
        throw "Profile does not exist: $profilePath"
    }

    $portFile = Get-ProfilePortFile -ProfilePath $profilePath
    $existing = Read-ProfilePortFile -PortFile $portFile
    if ($null -ne $existing) {
        return [int]$existing
    }

    $used = Get-UsedProfilePorts -ProfilesRoot $ProfilesRoot
    $candidate = [int]$BasePort
    while ($used.ContainsKey($candidate)) {
        $candidate += 1
    }

    Set-Content -LiteralPath $portFile -Value ([string]$candidate) -Encoding ASCII
    return [int]$candidate
}

function Get-ProfilePortFile {
    param([Parameter(Mandatory)] [string] $ProfilePath)
    return (Join-Path $ProfilePath $Script:PORT_FILE_NAME)
}

function Read-ProfilePortFile {
    param([Parameter(Mandatory)] [string] $PortFile)

    if (-not (Test-Path -LiteralPath $PortFile -PathType Leaf)) {
        return $null
    }

    $raw = (Get-Content -LiteralPath $PortFile -Raw -ErrorAction Stop).Trim()
    $port = 0
    if ([int]::TryParse($raw, [ref]$port) -and $port -gt 0 -and $port -lt 65536) {
        return [int]$port
    }
    return $null
}

function Get-UsedProfilePorts {
    param([Parameter(Mandatory)] [string] $ProfilesRoot)

    $used = @{}
    foreach ($entry in (Get-ProfileEntries -ProfilesRoot $ProfilesRoot)) {
        $portFile = Get-ProfilePortFile -ProfilePath $entry.Path
        $port = Read-ProfilePortFile -PortFile $portFile
        if ($null -ne $port) {
            $used[[int]$port] = $true
        }
    }
    return $used
}

function Write-Status {
    param(
        [string] $Message,
        [ValidateSet('info', 'ok', 'warn', 'err')] [string] $Level = 'info'
    )

    $prefix = switch ($Level) {
        'ok' { '[OK]   ' }
        'warn' { '[WARN] ' }
        'err' { '[ERR]  ' }
        default { '[..]   ' }
    }
    $color = switch ($Level) {
        'ok' { 'Green' }
        'warn' { 'Yellow' }
        'err' { 'Red' }
        default { 'Cyan' }
    }
    Write-Host ($prefix + $Message) -ForegroundColor $color
}

function Invoke-Opensquilla {
    param(
        [string] $Repo,
        [Parameter(Mandatory)] [string] $Profile,
        [Parameter(Mandatory)] [string[]] $Arguments,
        [switch] $CaptureOutput
    )

    $profileLeaf = Split-Path -Leaf $Profile
    $profileRoot = Split-Path -Parent $Profile
    if (-not (Test-ProfileName -Name $profileLeaf)) {
        throw "Invalid OpenSquilla profile name: $profileLeaf"
    }

    $previousHome = $env:OPENSQUILLA_HOME
    $previousProfile = $env:OPENSQUILLA_PROFILE
    $previousStateDir = $env:OPENSQUILLA_STATE_DIR
    $env:OPENSQUILLA_HOME = $profileRoot
    $env:OPENSQUILLA_PROFILE = $profileLeaf
    Remove-Item Env:\OPENSQUILLA_STATE_DIR -ErrorAction SilentlyContinue

    try {
        $cmd = Get-OpensquillaCommand -Repo $Repo
        switch ($cmd.Mode) {
            'installed' {
                if ($CaptureOutput) {
                    $output = & $cmd.Exe @Arguments 2>$null
                    return @{ ExitCode = $LASTEXITCODE; Output = $output }
                }
                & $cmd.Exe @Arguments
                return @{ ExitCode = $LASTEXITCODE; Output = $null }
            }
            'uv-run-repo' {
                Push-Location -LiteralPath $cmd.Repo
                try {
                    if ($CaptureOutput) {
                        $output = & uv run opensquilla @Arguments 2>$null
                        return @{ ExitCode = $LASTEXITCODE; Output = $output }
                    }
                    & uv run opensquilla @Arguments
                    return @{ ExitCode = $LASTEXITCODE; Output = $null }
                } finally {
                    Pop-Location
                }
            }
            default {
                throw 'opensquilla is not on PATH and no source checkout was auto-detected next to this script. Install OpenSquilla or pass -Repo.'
            }
        }
    } finally {
        if ($null -eq $previousHome) {
            Remove-Item Env:\OPENSQUILLA_HOME -ErrorAction SilentlyContinue
        } else {
            $env:OPENSQUILLA_HOME = $previousHome
        }
        if ($null -eq $previousProfile) {
            Remove-Item Env:\OPENSQUILLA_PROFILE -ErrorAction SilentlyContinue
        } else {
            $env:OPENSQUILLA_PROFILE = $previousProfile
        }
        if ($null -eq $previousStateDir) {
            Remove-Item Env:\OPENSQUILLA_STATE_DIR -ErrorAction SilentlyContinue
        } else {
            $env:OPENSQUILLA_STATE_DIR = $previousStateDir
        }
    }
}

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([Parameter(Mandatory)] [string] $Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function ConvertTo-XmlEscapedText {
    param([Parameter(Mandatory)] [string] $Value)
    return [System.Security.SecurityElement]::Escape($Value)
}
