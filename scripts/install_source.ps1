# install_source.ps1 - user-local OpenSquilla installer (no admin).
#
# Installer contract:
#   - installs into a user-owned prefix (never Program Files or system32)
#   - prefers uv tool install; falls back to pip --user; errors clearly if neither exists
#   - requires the Node.js version pinned by opensquilla-webui/.node-version,
#     runs npm ci + npm run build, and packages that exact Web UI
#   - defaults to the "recommended" runtime profile (memory + bundled v4 router)
#     and allows `$env:OPENSQUILLA_INSTALL_PROFILE="core"` to opt back down
#   - on Windows, best-effort installs Microsoft Visual C++ Redistributable
#     before the recommended router profile because onnxruntime requires it
#   - prints a post-install banner documenting the default bind
#     (127.0.0.1:18791) and the explicit opt-in required to expose the gateway
#     on the network (-Listen 0.0.0.0 or $env:OPENSQUILLA_LISTEN="0.0.0.0")
#   - adds an extra WARNING when the operator requested network exposure at
#     install time via $env:OPENSQUILLA_LISTEN="0.0.0.0"
#
# Dry-run: set $env:OPENSQUILLA_INSTALL_DRY_RUN="1" to print the install plan +
# banner without touching the system.

param(
    [string]$Profile = "",
    [string[]]$Extras = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# PowerShell 7 can promote a non-zero native exit to a terminating error
# before $LASTEXITCODE is inspected. This installer checks native commands
# explicitly so npm and the selected Python installer keep their original
# exit codes on every supported PowerShell host.
if ($null -ne (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue)) {
    Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false
}

# --- prefix resolution ------------------------------------------------------

if ($env:OPENSQUILLA_PREFIX) {
    $prefix = $env:OPENSQUILLA_PREFIX
} elseif ($env:LOCALAPPDATA) {
    $prefix = Join-Path $env:LOCALAPPDATA 'opensquilla'
} else {
    $prefix = Join-Path $HOME '.local'
}

$dryRun = $env:OPENSQUILLA_INSTALL_DRY_RUN -eq '1'
$webuiDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'opensquilla-webui'
$nodeVersionFile = Join-Path $webuiDir '.node-version'
if (-not (Test-Path $nodeVersionFile -PathType Leaf)) {
    Write-Error "install_source.ps1: required Node.js version file is missing: $nodeVersionFile"
    exit 1
}
try {
    $minimumNodeVersion = [version]((Get-Content $nodeVersionFile -Raw).Trim().TrimStart('v'))
} catch {
    Write-Error "install_source.ps1: could not parse the required Node.js version in $nodeVersionFile."
    exit 1
}
$script:isWindowsHost = if (Get-Variable IsWindows -ErrorAction SilentlyContinue) {
    $IsWindows
} else {
    $env:OS -eq 'Windows_NT'
}
$profile = if ($Profile) {
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
    Write-Error "install_source.ps1: unsupported extras: $($unknownExtras -join ', '). Supported extras: $($validExtras -join ', ')."
    exit 1
}

switch ($profile) {
    'core' { $targetExtras = @() }
    'minimal' { $profile = 'core'; $targetExtras = @() }
    'recommended' { $targetExtras = @('recommended') }
    default {
        Write-Error "install_source.ps1: unsupported OPENSQUILLA_INSTALL_PROFILE='$profile'. Supported profiles: core, recommended."
        exit 1
    }
}

$targetExtras += $installExtras
$installTarget = if ($targetExtras.Count -gt 0) {
    ".[$($targetExtras -join ',')]"
} else {
    '.'
}

function Test-SquillaRouterAssets {
    param(
        [switch]$WarnOnly
    )

    if ($profile -ne 'recommended') {
        return
    }

    $modelRoot = 'src/opensquilla/squilla_router/models'
    $required = @(
        "$modelRoot/v4.2_phase3_inference/lgbm_main.bin",
        "$modelRoot/v4.2_phase3_inference/router.runtime.yaml",
        "$modelRoot/v4.2_phase3_inference/mlp/model.onnx",
        "$modelRoot/v4.2_phase3_inference/features/tfidf.pkl",
        "$modelRoot/v4.2_phase3_inference/bge_onnx/model.onnx"
    )
    $pointerLine = 'version https://git-lfs.github.com/spec/v1'
    $missing = New-Object System.Collections.Generic.List[string]
    $pointers = New-Object System.Collections.Generic.List[string]

    foreach ($path in $required) {
        if (-not (Test-Path $path -PathType Leaf)) {
            $missing.Add($path)
            continue
        }
        $firstLine = Get-Content -Path $path -TotalCount 1 -ErrorAction SilentlyContinue
        if ($firstLine -eq $pointerLine) {
            $pointers.Add($path)
        }
    }

    if ($missing.Count -gt 0 -or $pointers.Count -gt 0) {
        if ($WarnOnly) {
            Write-Host 'install_source.ps1: dry-run note — real recommended install would fail until bundled squilla-router v4 assets are available in this checkout.'
        }
        else {
            Write-Error 'install_source.ps1: bundled squilla-router v4 assets are unavailable in this checkout.'
        }
        if ($missing.Count -gt 0) {
            $message = "install_source.ps1: missing squilla-router assets: $($missing -join ', ')"
            if ($WarnOnly) { Write-Host $message } else { Write-Error $message }
        }
        if ($pointers.Count -gt 0) {
            $message = "install_source.ps1: Git LFS pointer files detected: $($pointers -join ', ')"
            if ($WarnOnly) { Write-Host $message } else { Write-Error $message }
        }
        $lfsMessage = 'install_source.ps1: run `git lfs install` once, then `git lfs pull --include="src/opensquilla/squilla_router/models/**"`.'
        $coreMessage = 'install_source.ps1: or retry with `$env:OPENSQUILLA_INSTALL_PROFILE="core"` for the minimal runtime.'
        if ($WarnOnly) {
            Write-Host $lfsMessage
            Write-Host $coreMessage
            return
        }
        Write-Error $lfsMessage
        Write-Error $coreMessage
        exit 1
    }
}

function Build-WebUI {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        Write-Error "install_source.ps1: Node.js >= $minimumNodeVersion is required to build the Web UI from source. Install Node.js, or use an official wheel/Desktop installer (no Node.js required)."
        exit 1
    }

    $rawNodeVersion = (& $nodeCommand.Source --version 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or -not $rawNodeVersion) {
        Write-Error 'install_source.ps1: could not determine the installed Node.js version.'
        exit 1
    }
    try {
        $nodeVersion = [version]($rawNodeVersion.Trim().TrimStart('v'))
    } catch {
        Write-Error "install_source.ps1: could not parse Node.js version '$rawNodeVersion'."
        exit 1
    }
    if ($nodeVersion -lt $minimumNodeVersion) {
        Write-Error "install_source.ps1: Node.js >= $minimumNodeVersion is required; found $nodeVersion. Upgrade Node.js, or use an official wheel/Desktop installer (no Node.js required)."
        exit 1
    }

    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) {
        $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $npmCommand) {
        Write-Error 'install_source.ps1: npm is required to build the Web UI from source. Install npm, or use an official wheel/Desktop installer (no npm required).'
        exit 1
    }
    if (-not (Test-Path (Join-Path $webuiDir 'package-lock.json') -PathType Leaf)) {
        Write-Error "install_source.ps1: Web UI package lock is missing: $webuiDir\package-lock.json"
        exit 1
    }

    Write-Host 'install_source.ps1: installing locked Web UI dependencies (npm ci)'
    Push-Location $webuiDir
    try {
        & $npmCommand.Source ci
        $npmExitCode = $LASTEXITCODE
        if ($npmExitCode -ne 0) {
            [Console]::Error.WriteLine("install_source.ps1: npm ci failed with exit code $npmExitCode.")
            exit $npmExitCode
        }
        & $npmCommand.Source run build
        $npmExitCode = $LASTEXITCODE
        if ($npmExitCode -ne 0) {
            [Console]::Error.WriteLine("install_source.ps1: npm run build failed with exit code $npmExitCode.")
            exit $npmExitCode
        }
    } finally {
        Pop-Location
    }
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
    if (-not $script:isWindowsHost -or $profile -ne 'recommended') {
        return
    }
    if ($env:OPENSQUILLA_SKIP_VC_REDIST -eq '1') {
        Write-Host 'install_source.ps1: skipping Microsoft Visual C++ Redistributable check because OPENSQUILLA_SKIP_VC_REDIST=1.'
        return
    }
    if (Test-WindowsVCRedistInstalled) {
        Write-Host 'install_source.ps1: Microsoft Visual C++ Redistributable is already installed.'
        return
    }

    $redistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'install_source.ps1: Microsoft Visual C++ Redistributable not detected; installing with winget.'
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
            Write-Host 'install_source.ps1: Microsoft Visual C++ Redistributable installation completed.'
            return
        }
        Write-Warning "install_source.ps1: winget could not install Microsoft Visual C++ Redistributable (exit $LASTEXITCODE)."
    }

    Write-Warning 'OpenSquilla: Microsoft Visual C++ Redistributable 2015-2022 x64 is required for the bundled ONNX router.'
    Write-Warning 'OpenSquilla can still start with safe router fallback, but bundled ONNX model routing is disabled until this runtime is installed.'
    Write-Warning "If automatic installation fails, install it manually: $redistUrl"
    Write-Warning 'After installing, reopen PowerShell and restart OpenSquilla.'
}

# --- installer selection ----------------------------------------------------

$installer = $null
$installArgs = @()

# Probe the ambient python version once (used only for the pip fallback gate).
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$pythonOk = $false
if ($pythonCmd) {
    & python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
    $pythonOk = ($LASTEXITCODE -eq 0)
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
    $installer = 'uv'
    $installArgs = @('tool', 'install', '--python', '3.12', '--force', '--reinstall-package', 'opensquilla', $installTarget)
} elseif ($pythonOk) {
    $installer = 'pip'
    $installArgs = @('-m', 'pip', 'install', '--user', $installTarget)
} else {
    # No uv, and the ambient python is missing or older than 3.12. Do NOT
    # silently pip-install onto an unsupported interpreter: a broken
    # opensquilla makes coding mode fall back to manual edits. Fail loud.
    $pyver = if ($pythonCmd) { (& python -V 2>&1) } else { 'none' }
    Write-Error "install_source.ps1: cannot install - uv not found and python ($pyver) is older than 3.12. OpenSquilla requires Python >= 3.12. Install uv (it brings its own 3.12): 'irm https://astral.sh/uv/install.ps1 | iex', then re-run scripts/install_source.ps1."
    exit 1
}

$installCmd = if ($installer -eq 'uv') {
    "uv $($installArgs -join ' ')"
} else {
    "python $($installArgs -join ' ')"
}

# --- banner -----------------------------------------------------------------

function Write-Banner {
    @"
----------------------------------------------------------------------------
OpenSquilla installed via $installer -> $prefix (profile: $profile)
Extras: $(if ($installExtras.Count -gt 0) { $installExtras -join ', ' } else { 'none' })

Default gateway bind: 127.0.0.1:18791 (loopback only)
Network exposure is opt-in only. To expose the gateway on the network you
must use one of:
  - CLI flag:  opensquilla gateway run --listen 0.0.0.0
  - Env var:   `$env:OPENSQUILLA_LISTEN="0.0.0.0"; opensquilla gateway run

Reminder: only expose 0.0.0.0 behind a trusted reverse proxy or VPN. The
gateway's first-class auth assumes loopback-scope by default.
----------------------------------------------------------------------------
"@ | Write-Host
}

function Write-ListenWarning {
    @"
WARNING: you have selected network-exposed default - ensure you
   understand the blast radius. The gateway will bind to 0.0.0.0 and be
   reachable from every interface on this host.
"@ | Write-Host
}

# --- post-install PATH sanity (parity with install_source.sh) --------------

function Resolve-EntrypointDir {
    # Determine where the just-installed `opensquilla`/`gateway` entry points
    # landed, so we can warn when that directory is not on PATH. uv tool
    # install drops entry points in `uv tool dir --bin`; pip --user puts them
    # in the interpreter's Scripts dir. Both live outside the default PATH on
    # a clean Windows host - the exact failure mode `opensquilla onboard`
    # hits right after a "successful" install. Parity with install_source.sh,
    # which does the same absolute-path lookup on POSIX.
    if ($installer -eq 'uv') {
        $uvBin = $null
        try {
            $line = (& uv tool dir --bin 2>$null | Select-Object -First 1)
            if ($line) { $uvBin = $line.Trim() }
        } catch { }
        if ($uvBin -and (Test-Path (Join-Path $uvBin 'opensquilla.exe') -PathType Leaf)) {
            return $uvBin
        }
        $fallback = Join-Path $HOME '.local\bin'
        if (Test-Path (Join-Path $fallback 'opensquilla.exe') -PathType Leaf) {
            return $fallback
        }
        return $null
    } else {
        $scriptsDir = $null
        try {
            $line = (& python -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>$null | Select-Object -First 1)
            if ($line) { $scriptsDir = $line.Trim() }
        } catch { }
        if ($scriptsDir -and (Test-Path (Join-Path $scriptsDir 'opensquilla.exe') -PathType Leaf)) {
            return $scriptsDir
        }
        return $null
    }
}

function Test-DirOnUserPath {
    param([string]$Dir)
    if (-not $Dir) { return $false }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { return $false }
    $target = $Dir.TrimEnd('\')
    foreach ($entry in ($userPath -split ';')) {
        if ([string]::IsNullOrWhiteSpace($entry)) { continue }
        if ([string]::Equals($entry.TrimEnd('\'), $target, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Write-PathHint {
    # Verify the just-installed entry point is reachable from a fresh shell.
    # install_source.sh runs the same smoke check on POSIX; this brings the
    # PowerShell installer to parity so a "successful" install does not leave
    # the user with an unresolvable `opensquilla` command (see issue #500).
    $entryDir = Resolve-EntrypointDir
    if (-not $entryDir) {
        Write-Warning 'install_source.ps1: could not locate the installed `opensquilla` entry point to verify PATH.'
        Write-Warning "install_source.ps1: if `opensquilla` is not recognized, run 'uv tool update-shell' and open a new terminal."
        return
    }
    if (Test-DirOnUserPath -Dir $entryDir) {
        Write-Host "install_source.ps1: entry points are on PATH ($entryDir)."
        return
    }
    Write-Warning "install_source.ps1: entry points are NOT on PATH: $entryDir"
    Write-Warning 'install_source.ps1: `opensquilla` will not be found in a new terminal until this is fixed.'
    Write-Warning 'install_source.ps1: fix it with one of:'
    Write-Warning '    uv tool update-shell               # uv official PATH configurator (recommended)'
    $oneLiner = '[Environment]::SetEnvironmentVariable(''Path'', [Environment]::GetEnvironmentVariable(''Path'',''User'') + '';{0}'', ''User'')' -f $entryDir
    Write-Warning "    $oneLiner   # or add this dir to user PATH manually"
    Write-Warning "install_source.ps1: then open a new terminal and run 'opensquilla onboard'."
}

if ($dryRun) {
    Write-Host "install_source.ps1: dry-run — would require Node.js >= $minimumNodeVersion and npm"
    Write-Host "install_source.ps1: dry-run — would run in ${webuiDir}: npm ci"
    Write-Host "install_source.ps1: dry-run — would run in ${webuiDir}: npm run build"
    Write-Host "install_source.ps1: dry-run — would run: $installCmd"
    Write-Host "install_source.ps1: dry-run — prefix: $prefix"
    Test-SquillaRouterAssets -WarnOnly
    Write-Banner
    if ($env:OPENSQUILLA_LISTEN -eq '0.0.0.0') {
        Write-ListenWarning
    }
    exit 0
}

# --- execute ---------------------------------------------------------------

Test-SquillaRouterAssets
Build-WebUI
Install-WindowsVCRedistIfNeeded

Write-Host "install_source.ps1: installing via $installer into prefix $prefix"
Write-Host "install_source.ps1: running: $installCmd"
if ($installer -eq 'uv') {
    & uv @installArgs
} else {
    & python @installArgs
}
$installExitCode = $LASTEXITCODE
if ($installExitCode -ne 0) {
    [Console]::Error.WriteLine("install_source.ps1: install command failed with exit code $installExitCode.")
    [Console]::Error.WriteLine('install_source.ps1: Close any running OpenSquilla gateway or shell using the existing tool environment, then retry.')
    exit $installExitCode
}

# Write an install receipt to aid `opensquilla uninstall`. Best-effort.
try {
    $receiptHome = if ($env:OPENSQUILLA_STATE_DIR) { $env:OPENSQUILLA_STATE_DIR } else { Join-Path $HOME '.opensquilla' }
    $receiptMethod = if ($installer -eq 'uv') { 'uv-tool' } else { 'pip' }
    New-Item -ItemType Directory -Force -Path $receiptHome | Out-Null
    $receipt = [ordered]@{
        version        = 1
        install_method = $receiptMethod
        installed_at   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        entrypoints    = @()
        owned_paths    = @()
        data_root      = $receiptHome
    }
    $receipt | ConvertTo-Json | Set-Content -Path (Join-Path $receiptHome 'install-receipt.json') -Encoding utf8
} catch {
    # Receipt is optional; never fail the install over it.
}

# Smoke-check the just-installed entry point is reachable from a fresh shell.
# Runs after install only (dry-run exits above), matching install_source.sh.
Write-PathHint

Write-Banner
if ($env:OPENSQUILLA_LISTEN -eq '0.0.0.0') {
    Write-ListenWarning
}
