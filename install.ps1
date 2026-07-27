# install.ps1 - OpenSquilla release installer for Windows PowerShell.
#
# This script installs uv if needed, installs a release wheel with uv tool, then
# prints the explicit next steps. It does not run onboarding or start the gateway.

param(
    [string]$Version = "",
    [string]$Profile = "",
    [string[]]$Extras = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$defaultVersion = 'v0.5.0'
$repoSlug = if ($env:OPENSQUILLA_REPOSITORY) { $env:OPENSQUILLA_REPOSITORY } else { 'opensquilla/opensquilla' }
$pythonVersion = if ($env:OPENSQUILLA_PYTHON_VERSION) { $env:OPENSQUILLA_PYTHON_VERSION } else { '3.12' }
$originalPath = if ($env:Path) { $env:Path } else { '' }
$dryRun = $env:OPENSQUILLA_INSTALL_DRY_RUN -eq '1'
$script:isWindowsHost = if (Get-Variable IsWindows -ErrorAction SilentlyContinue) {
    $IsWindows
} else {
    $env:OS -eq 'Windows_NT'
}

if (-not $Version) {
    $Version = if ($env:OPENSQUILLA_VERSION) { $env:OPENSQUILLA_VERSION } else { $defaultVersion }
}

$profileName = if ($Profile) {
    $Profile
} elseif ($env:OPENSQUILLA_INSTALL_PROFILE) {
    $env:OPENSQUILLA_INSTALL_PROFILE
} else {
    'recommended'
}

$validExtras = @(
    'matrix',
    'matrix-e2e',
    'document-extras'
)

function Split-InstallExtras {
    param([string[]]$Values)

    $items = New-Object System.Collections.Generic.List[string]
    foreach ($value in $Values) {
        if (-not $value) {
            continue
        }
        foreach ($part in ($value -split '[,\s]+')) {
            $item = $part.Trim()
            if ($item -and -not $items.Contains($item)) {
                $items.Add($item)
            }
        }
    }
    return $items.ToArray()
}

$extraInputs = @()
if ($env:OPENSQUILLA_INSTALL_EXTRAS) {
    $extraInputs += $env:OPENSQUILLA_INSTALL_EXTRAS
}
$extraInputs += $Extras
$installExtras = @(Split-InstallExtras $extraInputs)
$unknownExtras = @($installExtras | Where-Object { $_ -notin $validExtras })
if ($unknownExtras.Count -gt 0) {
    Write-Error "install.ps1: unsupported extras: $($unknownExtras -join ', '). Supported extras: $($validExtras -join ', ')."
    exit 1
}

switch ($profileName) {
    'core' { $targetExtras = @() }
    'minimal' { $profileName = 'core'; $targetExtras = @() }
    'recommended' { $targetExtras = @('recommended') }
    default {
        Write-Error "install.ps1: unsupported OPENSQUILLA_INSTALL_PROFILE='$profileName'. Supported profiles: core, recommended."
        exit 1
    }
}

$targetExtras += $installExtras
$packageName = if ($targetExtras.Count -gt 0) {
    "opensquilla[$($targetExtras -join ',')]"
} else {
    'opensquilla'
}

function Test-ReleaseVersion {
    param([string]$Value)
    return $Value -match '^v?\d+\.\d+\.\d+((a|b|rc)\d+)?$'
}

if ($Version -notin @('latest', 'stable') -and -not (Test-ReleaseVersion $Version)) {
    Write-Error "install.ps1: unsupported OPENSQUILLA_VERSION='$Version'. The release installer only supports latest, stable, or release versions like v0.5.0. Use git clone plus scripts/install_source.ps1 for main, dev, branch, or source installs."
    exit 1
}

switch -Regex ($Version) {
    '^(latest|stable)$' {
        try {
            $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/$repoSlug/releases/latest" -Headers @{ 'User-Agent' = 'OpenSquilla installer' }
        } catch {
            Write-Error "install.ps1: failed to resolve latest release for $repoSlug. $($_.Exception.Message)"
            exit 1
        }
        $releaseTag = [string]$latest.tag_name
        if (-not (Test-ReleaseVersion $releaseTag)) {
            Write-Error "install.ps1: latest release tag '$releaseTag' is not a supported release version."
            exit 1
        }
        $releaseVersion = if ($releaseTag.StartsWith('v')) { $releaseTag.Substring(1) } else { $releaseTag }
        $wheelUrl = "https://github.com/$repoSlug/releases/download/$releaseTag/opensquilla-$releaseVersion-py3-none-any.whl"
        $displayVersion = $releaseTag
        break
    }
    '^v' {
        $releaseVersion = $Version.Substring(1)
        $wheelUrl = "https://github.com/$repoSlug/releases/download/$Version/opensquilla-$releaseVersion-py3-none-any.whl"
        $displayVersion = $Version
        break
    }
    default {
        $releaseVersion = $Version
        $releaseTag = "v$releaseVersion"
        $wheelUrl = "https://github.com/$repoSlug/releases/download/$releaseTag/opensquilla-$releaseVersion-py3-none-any.whl"
        $displayVersion = $releaseTag
    }
}

$installSpec = "$packageName @ $wheelUrl"

function Resolve-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidateDirs = @(
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:USERPROFILE '.cargo\bin')
    )
    $env:Path = ($candidateDirs -join ';') + ';' + $env:Path

    foreach ($dir in $candidateDirs) {
        $candidate = Join-Path $dir 'uv.exe'
        if (Test-Path $candidate -PathType Leaf) {
            return $candidate
        }
    }

    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
}

function Install-Uv {
    Write-Host 'install.ps1: uv not found; installing uv first.'
    Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' | Invoke-Expression
}

function Test-WindowsVCRedistInstalled {
    if (-not $script:isWindowsHost) {
        return $true
    }

    $runtimeKeys = @(
        'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    )
    foreach ($key in $runtimeKeys) {
        if (-not (Test-Path $key)) {
            continue
        }
        $runtime = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
        if ($runtime -and $runtime.Installed -eq 1 -and $runtime.Major -ge 14) {
            return $true
        }
    }
    return $false
}

function Install-WindowsVCRedistIfNeeded {
    if (-not $script:isWindowsHost -or $profileName -ne 'recommended') {
        return
    }
    if ($env:OPENSQUILLA_SKIP_VC_REDIST -eq '1') {
        Write-Host 'install.ps1: skipping Microsoft Visual C++ Redistributable check because OPENSQUILLA_SKIP_VC_REDIST=1.'
        return
    }
    if (Test-WindowsVCRedistInstalled) {
        Write-Host 'install.ps1: Microsoft Visual C++ Redistributable is already installed.'
        return
    }

    $redistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'install.ps1: Microsoft Visual C++ Redistributable not detected; installing with winget.'
        $wingetArgs = @(
            'install',
            '--id',
            'Microsoft.VCRedist.2015+.x64',
            '--exact',
            '--silent',
            '--accept-package-agreements',
            '--accept-source-agreements'
        )
        & winget @wingetArgs
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'install.ps1: Microsoft Visual C++ Redistributable installation completed.'
            return
        }
        Write-Warning "install.ps1: winget could not install Microsoft Visual C++ Redistributable (exit $LASTEXITCODE)."
    }

    Write-Warning 'OpenSquilla: Microsoft Visual C++ Redistributable 2015-2022 x64 is required for the bundled ONNX router.'
    Write-Warning 'OpenSquilla can still start with safe router fallback, but bundled ONNX model routing is disabled until this runtime is installed.'
    Write-Warning "If automatic installation fails, install it manually: $redistUrl"
    Write-Warning 'After installing, reopen PowerShell and restart OpenSquilla.'
}

if ($dryRun) {
    Write-Host "install.ps1: dry-run - would install OpenSquilla $displayVersion"
    Write-Host "install.ps1: dry-run - would run: uv tool install --python $pythonVersion --force --reinstall-package opensquilla `"$installSpec`""
    exit 0
}

$uvBin = Resolve-Uv
if (-not $uvBin) {
    Install-Uv
    $uvBin = Resolve-Uv
}

if (-not $uvBin) {
    Write-Error "install.ps1: uv was not found after installation. Restart PowerShell or add '$env:USERPROFILE\.local\bin' to PATH, then retry."
    exit 1
}

Install-WindowsVCRedistIfNeeded

Write-Host "install.ps1: installing OpenSquilla $displayVersion ($profileName)"
& $uvBin tool install --python $pythonVersion --force --reinstall-package opensquilla $installSpec
if ($LASTEXITCODE -ne 0) {
    Write-Error "install.ps1: install command failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

$toolBinDir = ''
try {
    $toolBinDir = (& $uvBin tool dir --bin 2>$null).Trim()
} catch {
    $toolBinDir = ''
}

# Write an install receipt to aid `opensquilla uninstall`. Best-effort.
try {
    $receiptHome = if ($env:OPENSQUILLA_STATE_DIR) { $env:OPENSQUILLA_STATE_DIR } else { Join-Path $HOME '.opensquilla' }
    $toolDir = ''
    try { $toolDir = (& $uvBin tool dir 2>$null).Trim() } catch { $toolDir = '' }
    New-Item -ItemType Directory -Force -Path $receiptHome | Out-Null
    $receipt = [ordered]@{
        version        = 1
        install_method = 'uv-tool'
        installed_at   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        entrypoints    = @("$toolBinDir/opensquilla", "$toolBinDir/gateway")
        owned_paths    = @("$toolDir/opensquilla", "$toolBinDir/opensquilla", "$toolBinDir/gateway")
        data_root      = $receiptHome
    }
    $receipt | ConvertTo-Json | Set-Content -Path (Join-Path $receiptHome 'install-receipt.json') -Encoding utf8
} catch {
    # Receipt is optional; never fail the install over it.
}

@"
----------------------------------------------------------------------------
OpenSquilla installed from $displayVersion.

Next steps:
  opensquilla onboard
  opensquilla gateway run

Default gateway bind: 127.0.0.1:18791 (loopback only).
Do not expose the gateway on 0.0.0.0 unless it is behind a trusted reverse
proxy or VPN.
----------------------------------------------------------------------------
"@ | Write-Host

if ($toolBinDir -and -not (($originalPath -split ';') -contains $toolBinDir)) {
    @"

PATH note:
  Your current PowerShell may not find 'opensquilla' until PATH is refreshed.
  Run this command, then retry the next steps:

    `$env:Path = "$toolBinDir;" + `$env:Path

  Or open a new PowerShell window.
"@ | Write-Host
}
