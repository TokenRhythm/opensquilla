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

if (-not $SignToolPath) {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) { throw 'signtool.exe was not found on PATH.' }
    $SignToolPath = $command.Source
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
