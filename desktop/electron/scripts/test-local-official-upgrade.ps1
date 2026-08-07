[CmdletBinding()]
param(
    [string]$LocalInstaller = "",
    [string]$TargetVersion = "0.5.3-local.2",
    [switch]$KeepTemporaryFiles
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktopRoot = Split-Path -Parent $scriptRoot
$repoRoot = Resolve-Path (Join-Path $desktopRoot "..\..")
$artifactRoot = Join-Path $repoRoot "artifacts\local-upgrade"
$reportJson = Join-Path $artifactRoot "upgrade-report.json"
$reportMarkdown = Join-Path $artifactRoot "upgrade-report.md"
$officialVersion = "0.5.2"
$localVersion = $TargetVersion
$officialAssetName = "OpenSquilla-$officialVersion-win-x64.exe"
$officialInstaller = Join-Path $artifactRoot $officialAssetName
$checksumFile = Join-Path $artifactRoot "SHA256SUMS-v$officialVersion"
$releaseApi = "https://api.github.com/repos/opensquilla/opensquilla/releases/latest"
$releaseBase = "https://github.com/opensquilla/opensquilla/releases/download/v$officialVersion"
$nodeExe = Join-Path $desktopRoot "runtime\developer\windows-x64\node\node.exe"
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$probeScript = Join-Path $scriptRoot "probe-local-official-upgrade.mjs"
$profileScript = Join-Path $scriptRoot "seed-upgrade-profile.py"

New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
if (-not $LocalInstaller) {
    $LocalInstaller = Join-Path $repoRoot "dist\desktop-electron\OpenSquilla-$localVersion-win-x64.exe"
}
$LocalInstaller = [IO.Path]::GetFullPath($LocalInstaller)

foreach ($required in @($LocalInstaller, $nodeExe, $pythonExe, $probeScript, $profileScript)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required file is missing: $required"
    }
}

function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
        -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "$FilePath exited with code $($process.ExitCode)"
    }
}

function Invoke-Probe {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$UserData,
        [Parameter(Mandatory = $true)][string]$IsolatedHome,
        [Parameter(Mandatory = $true)][string]$State,
        [switch]$RequireChatNew
    )
    $arguments = @(
        $probeScript,
        "--executable", $Executable,
        "--user-data-dir", $UserData,
        "--home", $IsolatedHome,
        "--state-dir", $State
    )
    if ($RequireChatNew) {
        $arguments += "--require-chat-new"
    }
    $output = & $nodeExe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged client probe failed with exit code $LASTEXITCODE"
    }
    return ($output | Select-Object -Last 1 | ConvertFrom-Json)
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("opensquilla-upgrade-rehearsal-" + [Guid]::NewGuid().ToString("N"))
$installRoot = Join-Path $tempRoot "app"
$userDataRoot = Join-Path $tempRoot "chromium"
$isolatedHome = Join-Path $tempRoot "home"
$profileHome = Join-Path $userDataRoot "opensquilla"
$workspaceRoot = Join-Path $profileHome "workspace"
$stateRoot = Join-Path $profileHome "state"
$configPath = Join-Path $profileHome "config.toml"
$sessionDatabase = Join-Path $stateRoot "sessions.db"
$tokenFile = Join-Path $tempRoot "synthetic-token.txt"
$startedAt = [DateTimeOffset]::UtcNow
$report = [ordered]@{
    ok = $false
    startedAt = $startedAt.ToString("o")
    fromVersion = $officialVersion
    toVersion = $localVersion
    officialRelease = "https://github.com/opensquilla/opensquilla/releases/tag/v$officialVersion"
    officialInstallerSha256 = $null
    localInstallerSha256 = $null
    profilePreserved = $false
    credentialBytesChanged = $false
    configBytesChanged = $false
    tasksPreserved = $false
    tokenPreserved = $false
    defaultRoute = $false
    desktopLoopbackOnly = $false
    oldProbe = $null
    newProbe = $null
    listeningAddresses = @()
    isolatedRoot = $tempRoot
    cleanupCompleted = $false
    error = $null
}

try {
    $release = Invoke-RestMethod -Uri $releaseApi -Headers @{ "User-Agent" = "OpenSquilla-local-upgrade-rehearsal" }
    if ($release.tag_name -ne "v$officialVersion" -or $release.draft -or $release.prerelease) {
        throw "Latest stable release must be v$officialVersion; received $($release.tag_name)"
    }

    if (-not (Test-Path -LiteralPath $checksumFile -PathType Leaf)) {
        Invoke-WebRequest -Uri "$releaseBase/SHA256SUMS" -OutFile $checksumFile
    }
    if (-not (Test-Path -LiteralPath $officialInstaller -PathType Leaf)) {
        Invoke-WebRequest -Uri "$releaseBase/$officialAssetName" -OutFile $officialInstaller
    }
    $checksumLine = Get-Content -LiteralPath $checksumFile |
        Where-Object { $_ -match [Regex]::Escape($officialAssetName) } |
        Select-Object -First 1
    if (-not $checksumLine -or $checksumLine -notmatch "^([0-9a-fA-F]{64})\s+\*?$([Regex]::Escape($officialAssetName))$") {
        throw "SHA256SUMS does not contain an exact entry for $officialAssetName"
    }
    $expectedOfficialHash = $Matches[1].ToUpperInvariant()
    $actualOfficialHash = (Get-FileHash -LiteralPath $officialInstaller -Algorithm SHA256).Hash
    if ($actualOfficialHash -ne $expectedOfficialHash) {
        throw "Official installer SHA-256 mismatch"
    }
    $report.officialInstallerSha256 = $actualOfficialHash
    $report.localInstallerSha256 = (Get-FileHash -LiteralPath $LocalInstaller -Algorithm SHA256).Hash

    New-Item -ItemType Directory -Force -Path $installRoot, $userDataRoot, $isolatedHome, $profileHome, $workspaceRoot, $stateRoot | Out-Null
    Invoke-CheckedProcess -FilePath $officialInstaller -ArgumentList @("/S", "/D=$installRoot")
    $installedExecutable = Get-ChildItem -LiteralPath $installRoot -Filter "OpenSquilla.exe" -File -Recurse |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $installedExecutable) {
        throw "Official installer did not create OpenSquilla.exe in the isolated install root"
    }

    $oldProbe = Invoke-Probe -Executable $installedExecutable -UserData $userDataRoot -IsolatedHome $isolatedHome -State $stateRoot
    if ($oldProbe.version -ne $officialVersion) {
        throw "Official client reported version $($oldProbe.version), expected $officialVersion"
    }
    $report.oldProbe = $oldProbe

    & $pythonExe $profileScript set-full --config $configPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not set the isolated upgrade profile to full access"
    }
    & $pythonExe $profileScript seed --database $sessionDatabase --token-file $tokenFile
    if ($LASTEXITCODE -ne 0) {
        throw "Synthetic profile seeding failed with exit code $LASTEXITCODE"
    }
    $credentialPath = Join-Path $userDataRoot "desktop-credential.json"
    foreach ($profileFile in @($credentialPath, $configPath, $sessionDatabase, $tokenFile)) {
        if (-not (Test-Path -LiteralPath $profileFile -PathType Leaf)) {
            throw "Expected isolated profile file is missing: $profileFile"
        }
    }
    $credentialHashBefore = (Get-FileHash -LiteralPath $credentialPath -Algorithm SHA256).Hash
    $configHashBefore = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
    $credentialBefore = Get-Content -LiteralPath $credentialPath -Raw | ConvertFrom-Json

    Invoke-CheckedProcess -FilePath $LocalInstaller -ArgumentList @("/S", "/D=$installRoot")
    $installedExecutable = Get-ChildItem -LiteralPath $installRoot -Filter "OpenSquilla.exe" -File -Recurse |
        Select-Object -First 1 -ExpandProperty FullName
    $newProbe = Invoke-Probe -Executable $installedExecutable -UserData $userDataRoot -IsolatedHome $isolatedHome -State $stateRoot -RequireChatNew
    if ($newProbe.version -ne $localVersion) {
        throw "Updated client reported version $($newProbe.version), expected $localVersion"
    }
    $report.newProbe = $newProbe
    $report.defaultRoute = $newProbe.route -eq "/control/chat/new"

    & $pythonExe $profileScript verify --database $sessionDatabase --token-file $tokenFile
    if ($LASTEXITCODE -ne 0) {
        throw "Synthetic task/token verification failed with exit code $LASTEXITCODE"
    }
    $report.tasksPreserved = $true
    $report.tokenPreserved = $true
    $credentialHashAfter = (Get-FileHash -LiteralPath $credentialPath -Algorithm SHA256).Hash
    $configHashAfter = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
    $credentialAfter = Get-Content -LiteralPath $credentialPath -Raw | ConvertFrom-Json
    $configAfter = Get-Content -LiteralPath $configPath -Raw
    $report.credentialBytesChanged = $credentialHashBefore -ne $credentialHashAfter
    $report.configBytesChanged = $configHashBefore -ne $configHashAfter
    $credentialSemanticsPreserved = (
        $credentialBefore.provider -eq $credentialAfter.provider -and
        $credentialBefore.model -eq $credentialAfter.model -and
        $credentialBefore.baseUrl -eq $credentialAfter.baseUrl -and
        $credentialBefore.searchProvider -eq $credentialAfter.searchProvider
    )
    $fullAccessConfigPreserved = (
        $configAfter -match '(?ms)^\[sandbox\].*?^run_mode\s*=\s*"full"'
    )
    $report.profilePreserved = (
        $credentialSemanticsPreserved -and
        $fullAccessConfigPreserved
    )

    $addresses = @($newProbe.listeningAddresses)
    $report.listeningAddresses = $addresses
    $report.desktopLoopbackOnly = (
        $addresses.Count -gt 0 -and
        @($addresses | Where-Object { $_ -notin @("127.0.0.1", "::1") }).Count -eq 0
    )
    $report.ok = (
        $report.profilePreserved -and
        $report.tasksPreserved -and
        $report.tokenPreserved -and
        $report.defaultRoute -and
        $report.desktopLoopbackOnly
    )
    if (-not $report.ok) {
        throw "One or more upgrade preservation or network assertions failed"
    }
}
catch {
    $report.error = $_.Exception.Message
    throw
}
finally {
    $uninstaller = Get-ChildItem -LiteralPath $installRoot -Filter "Uninstall*.exe" -File -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if ($uninstaller) {
        try {
            Invoke-CheckedProcess -FilePath $uninstaller -ArgumentList @("/S")
        }
        catch {
            if (-not $report.error) {
                $report.error = "Cleanup failed: $($_.Exception.Message)"
            }
        }
    }
    if (-not $KeepTemporaryFiles -and (Test-Path -LiteralPath $tempRoot)) {
        $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
        $expectedPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if (-not $resolvedTemp.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not ([IO.Path]::GetFileName($resolvedTemp)).StartsWith("opensquilla-upgrade-rehearsal-")) {
            throw "Refusing to remove an unverified temporary root: $resolvedTemp"
        }
        [IO.Directory]::Delete($resolvedTemp, $true)
        $report.cleanupCompleted = $true
    }
    $report.finishedAt = [DateTimeOffset]::UtcNow.ToString("o")
    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    $jsonText = (($report | ConvertTo-Json -Depth 12) -replace "`r`n", "`n") + "`n"
    [IO.File]::WriteAllText($reportJson, $jsonText, $utf8NoBom)
    $markdownText = @"
# Local official-to-local upgrade rehearsal

- Result: **$($report.ok)**
- Official source: v$officialVersion
- Local target: $localVersion
- Profile preserved: $($report.profilePreserved)
- Credential bytes changed by migration: $($report.credentialBytesChanged)
- Config bytes changed by migration: $($report.configBytesChanged)
- Synthetic task preserved: $($report.tasksPreserved)
- Synthetic named token preserved: $($report.tokenPreserved)
- Default route `/control/chat/new`: $($report.defaultRoute)
- Desktop loopback-only: $($report.desktopLoopbackOnly)
- Listening addresses: $($report.listeningAddresses -join ", ")
- Temporary roots cleaned: $($report.cleanupCompleted)
- Official SHA-256: $($report.officialInstallerSha256)
- Local SHA-256: $($report.localInstallerSha256)
- Error: $($report.error)
"@
    [IO.File]::WriteAllText(
        $reportMarkdown,
        (($markdownText.TrimEnd()) -replace "`r`n", "`n") + "`n",
        $utf8NoBom
    )
}
