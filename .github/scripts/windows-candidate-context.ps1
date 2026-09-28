[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceSha,
  [ValidatePattern('^([0-9]+)?$')][string]$SourceRunId = '',
  [string]$WorkflowSha = $env:GITHUB_WORKFLOW_SHA
)

$ErrorActionPreference = 'Stop'
if ($SourceRunId) {
  $producer = gh api "repos/$env:GITHUB_REPOSITORY/actions/runs/$SourceRunId" | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or $producer.path -ne '.github/workflows/wheelhouse-release.yml') {
    throw 'Candidate must come from a Release Assets run.'
  }
  # The producer run identifies its workflow revision. The consumer may run
  # newer verification tooling; its workflow SHA is not the producer's SHA.
  $WorkflowSha = $producer.head_sha
}
if ($WorkflowSha -notmatch '^[0-9a-f]{40}$') { throw 'Expected workflow SHA is required.' }
$object = "${SourceSha}:desktop/electron/package.json"
git cat-file -e $object 2>$null
if ($LASTEXITCODE -ne 0) {
  $null = git fetch --no-tags --depth=1 origin $SourceSha
  if ($LASTEXITCODE -ne 0) { throw 'Unable to read the fixed candidate source.' }
}
$packageText = git show $object
if ($LASTEXITCODE -ne 0) { throw 'Candidate package metadata is missing.' }
$package = ($packageText -join "`n") | ConvertFrom-Json
if (-not $package.version) { throw 'Candidate source version is missing.' }
[pscustomobject]@{ WorkflowSha = $WorkflowSha; Version = [string]$package.version }
