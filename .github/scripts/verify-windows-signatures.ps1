[CmdletBinding()]
param(
    [string]$ArtifactRoot = '',
    [string]$InstallerPath = '',
    [string]$InstalledRoot = '',
    [string]$SignToolPath = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$policyPath = Join-Path $repoRoot '.github/signing/windows-signing-policy.json'
$policy = Get-Content -LiteralPath $policyPath -Raw | ConvertFrom-Json
$expectedThumbprint = ([string]$policy.certificateSha1).ToUpperInvariant()
$expectedPublisher = [string]$policy.publisherSubjectContains

function Resolve-SignToolFile([string]$Path, [string]$Source) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Source does not point to a SignTool file: $Path"
    }
    return (Get-Item -LiteralPath $Path).FullName
}

if ($PSBoundParameters.ContainsKey('SignToolPath')) {
    $SignToolPath = Resolve-SignToolFile $SignToolPath '-SignToolPath'
} elseif ($env:SIGNTOOL_PATH) {
    $SignToolPath = Resolve-SignToolFile $env:SIGNTOOL_PATH 'SIGNTOOL_PATH'
} else {
    $command = Get-Command signtool.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        $SignToolPath = $command.Source
    } else {
        # Download-audit jobs do not inherit the signing job's PATH. Locate the
        # latest installed Windows 10 SDK's x64 verifier without signing secrets.
        $sdkTools = @(
            foreach ($programFiles in @(${env:ProgramFiles(x86)}, $env:ProgramFiles) | Select-Object -Unique) {
                if (-not $programFiles) { continue }
                $sdkBin = Join-Path $programFiles 'Windows Kits/10/bin'
                if (-not (Test-Path -LiteralPath $sdkBin -PathType Container)) { continue }
                foreach ($directory in Get-ChildItem -LiteralPath $sdkBin -Directory) {
                    $version = $null
                    if (-not [version]::TryParse($directory.Name, [ref]$version)) { continue }
                    $candidate = Join-Path $directory.FullName 'x64/signtool.exe'
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                        [pscustomobject]@{ Version = $version; Path = $candidate }
                    }
                }
            }
        )
        $latest = $sdkTools | Sort-Object Version -Descending | Select-Object -First 1
        if ($null -eq $latest) {
            throw 'signtool.exe was not found on PATH or in a Windows 10 SDK x64 directory. Install the Windows SDK or set SIGNTOOL_PATH.'
        }
        $SignToolPath = $latest.Path
    }
}

$targets = [ordered]@{}
if (-not $ArtifactRoot -and -not $InstallerPath -and -not $InstalledRoot) {
    $ArtifactRoot = 'dist/desktop-electron'
}
if ($ArtifactRoot) {
    $root = (Resolve-Path -LiteralPath $ArtifactRoot).Path
    $installers = @(Get-ChildItem -LiteralPath $root -Filter 'OpenSquilla-*-win-x64.exe' -File)
    if ($installers.Count -ne 1) {
        throw "Expected exactly one Windows installer in $root; got $($installers.Count)."
    }
    $targets['NSIS installer'] = $installers[0].FullName
    $targets['OpenSquilla executable'] = Join-Path $root 'win-unpacked/OpenSquilla.exe'
    $targets['Packaged gateway executable'] = Join-Path $root 'win-unpacked/resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway.exe'
    $targets['Elevation helper executable'] = Join-Path $root 'win-unpacked/resources/elevate.exe'
}
if ($InstallerPath) {
    $targets['NSIS installer'] = (Resolve-Path -LiteralPath $InstallerPath).Path
}
if ($InstalledRoot) {
    $installed = (Resolve-Path -LiteralPath $InstalledRoot).Path
    $targets['Installed OpenSquilla executable'] = Join-Path $installed 'OpenSquilla.exe'
    $targets['Installed gateway executable'] = Join-Path $installed 'resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway.exe'
    $targets['Installed elevation helper'] = Join-Path $installed 'resources/elevate.exe'
    $uninstallers = @(Get-ChildItem -LiteralPath $installed -Filter 'Uninstall*.exe' -File)
    if ($uninstallers.Count -ne 1) {
        throw "Expected exactly one installed uninstaller in $installed; got $($uninstallers.Count)."
    }
    $targets['Installed uninstaller'] = $uninstallers[0].FullName
}

$results = foreach ($entry in $targets.GetEnumerator()) {
    if (-not (Test-Path -LiteralPath $entry.Value -PathType Leaf)) {
        throw "$($entry.Key) is missing: $($entry.Value)"
    }
    & $SignToolPath verify /pa /all /v /tw $entry.Value
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool verification failed for $($entry.Key): $($entry.Value)"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $entry.Value
    if ($signature.Status -ne 'Valid') {
        throw "$($entry.Key) Authenticode status is $($signature.Status): $($signature.StatusMessage)"
    }
    if ($signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $expectedThumbprint) {
        throw "$($entry.Key) certificate thumbprint is unexpected: $($signature.SignerCertificate.Thumbprint)"
    }
    if ($signature.SignerCertificate.Subject -notlike "*$expectedPublisher*") {
        throw "$($entry.Key) publisher is unexpected: $($signature.SignerCertificate.Subject)"
    }
    if ($null -eq $signature.TimeStamperCertificate) {
        throw "$($entry.Key) does not have an Authenticode timestamp certificate."
    }
    [pscustomobject]@{
        Label = $entry.Key
        Path = $entry.Value
        Status = [string]$signature.Status
        Thumbprint = $signature.SignerCertificate.Thumbprint
        Publisher = $signature.SignerCertificate.Subject
        TimestampPublisher = $signature.TimeStamperCertificate.Subject
    }
}

$results | Format-Table Label, Status, Thumbprint, Path -AutoSize
