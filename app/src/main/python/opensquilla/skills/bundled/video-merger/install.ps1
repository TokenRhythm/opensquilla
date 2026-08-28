# video-merger install script for Windows (PowerShell).
#
# Companion to install.sh (macOS/Linux). Installs ffmpeg if missing, verifies
# Python 3.8+, and prints the absolute ffmpeg path so the user can pass it via
# --ffmpeg-path if Windows subprocess PATH inheritance is unreliable.
#
# Usage:
#   pwsh -ExecutionPolicy Bypass -File install.ps1
#   # or, when execution policy already allows it:
#   .\install.ps1
#
# Environment overrides:
#   $env:OPENSQUILLA_FFMPEG_INSTALLER  -> winget (default) | choco | scoop | skip
#   $env:OPENSQUILLA_SKIP_PYTHON_CHECK -> 1 to skip python detection

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Section {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Resolve-Ffmpeg {
    # 1. on PATH?
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    # 2. winget Gyan.FFmpeg (the default installer below)
    $wingetGlob = Join-Path $env:LOCALAPPDATA `
        'Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_*\ffmpeg-*-full_build\bin\ffmpeg.exe'
    $hit = Get-ChildItem -Path $wingetGlob -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($hit) {
        return $hit.FullName
    }

    # 3. scoop / chocolatey default locations
    $candidates = @(
        (Join-Path $env:USERPROFILE 'scoop\apps\ffmpeg\current\bin\ffmpeg.exe'),
        'C:\ProgramData\chocolatey\bin\ffmpeg.exe',
        'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
        'C:\ffmpeg\bin\ffmpeg.exe'
    )
    foreach ($p in $candidates) {
        if (Test-Path $p -PathType Leaf) {
            return (Resolve-Path $p).Path
        }
    }
    return $null
}

function Install-Ffmpeg {
    $installer = $env:OPENSQUILLA_FFMPEG_INSTALLER
    if (-not $installer) { $installer = 'winget' }

    switch ($installer) {
        'skip' {
            Write-Warning "OPENSQUILLA_FFMPEG_INSTALLER=skip — assuming you'll install ffmpeg manually."
            return
        }
        'winget' {
            if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
                Write-Warning 'winget not found. Install via App Installer from the Microsoft Store, or set $env:OPENSQUILLA_FFMPEG_INSTALLER="choco"/"scoop".'
                throw 'winget unavailable'
            }
            Write-Section 'Installing ffmpeg via winget (Gyan.FFmpeg)'
            winget install --id Gyan.FFmpeg --exact --silent `
                --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                throw "winget install failed with exit code $LASTEXITCODE"
            }
        }
        'choco' {
            if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
                throw 'choco not found. Install Chocolatey first: https://chocolatey.org/install'
            }
            Write-Section 'Installing ffmpeg via Chocolatey'
            choco install ffmpeg -y
            if ($LASTEXITCODE -ne 0) {
                throw "choco install failed with exit code $LASTEXITCODE"
            }
        }
        'scoop' {
            if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
                throw 'scoop not found. Install Scoop first: https://scoop.sh'
            }
            Write-Section 'Installing ffmpeg via Scoop'
            scoop install ffmpeg
            if ($LASTEXITCODE -ne 0) {
                throw "scoop install failed with exit code $LASTEXITCODE"
            }
        }
        default {
            throw "Unsupported OPENSQUILLA_FFMPEG_INSTALLER='$installer'. Use winget, choco, scoop, or skip."
        }
    }
}

function Test-PythonVersion {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        $py = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $py) {
        throw "Python 3.8+ not found on PATH. Install Python 3.12 from https://www.python.org/downloads/windows/"
    }
    $version = & $py.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0 -or -not $version) {
        throw "Could not read Python version from $($py.Source)"
    }
    $parts = $version.Trim().Split('.')
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 8)) {
        throw "Python $version detected at $($py.Source); video-merger requires Python 3.8+."
    }
    Write-Host "Python $version found at $($py.Source)"
}

# ---- main ------------------------------------------------------------------

Write-Section 'video-merger Windows installer'

$existing = Resolve-Ffmpeg
if ($existing) {
    Write-Host "ffmpeg already available: $existing"
} else {
    try {
        Install-Ffmpeg
    } catch {
        Write-Error "ffmpeg installation failed: $($_.Exception.Message)"
        Write-Error 'Install ffmpeg manually from https://www.gyan.dev/ffmpeg/builds/ and re-run.'
        exit 1
    }
    $existing = Resolve-Ffmpeg
    if (-not $existing) {
        Write-Error 'ffmpeg installed but the binary could not be located on disk. Reopen PowerShell so PATH refreshes, then re-run.'
        exit 1
    }
    Write-Host "ffmpeg installed at: $existing"
}

if ($env:OPENSQUILLA_SKIP_PYTHON_CHECK -ne '1') {
    Test-PythonVersion
}

# Pre-resolve ffprobe alongside ffmpeg (same dir) for the caller's convenience.
$ffprobePath = Join-Path (Split-Path -Parent $existing) 'ffprobe.exe'
if (-not (Test-Path $ffprobePath -PathType Leaf)) {
    $ffprobePath = '(not co-located; pass --ffprobe-path explicitly if needed)'
}

@"

----------------------------------------------------------------------------
video-merger Windows dependencies are ready.

ffmpeg : $existing
ffprobe: $ffprobePath

If a freshly opened PowerShell does not find ffmpeg on PATH (winget needs a
session restart), call merge.py with explicit paths:

    python scripts\merge.py ``
        --input  <segments-dir> ``
        --output <out.mp4> ``
        --ffmpeg-path  "$existing" ``
        --ffprobe-path "$ffprobePath"

Or persist the bin directory in your user PATH via System Properties ->
Environment Variables -> Path.
----------------------------------------------------------------------------
"@ | Write-Host
