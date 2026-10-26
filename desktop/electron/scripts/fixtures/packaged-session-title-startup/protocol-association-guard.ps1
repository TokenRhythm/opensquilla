param(
  [Parameter(Mandatory=$true)][ValidateSet('snapshot','restore','check')][string]$Action,
  [Parameter(Mandatory=$true)][string]$EvidenceDirectory,
  [Parameter(Mandatory=$true)][string]$BaselineExecutable,
  [Parameter(Mandatory=$true)][string]$CandidateExecutable
)
$ErrorActionPreference = 'Stop'
$keyPath = 'Software\Classes\opensquilla'
$snapshotPath = Join-Path $EvidenceDirectory 'protocol-association-before.json'
$knownExecutables = @(
  [System.IO.Path]::GetFullPath($BaselineExecutable),
  [System.IO.Path]::GetFullPath($CandidateExecutable)
)

function Read-Association {
  $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath, $false)
  if ($null -eq $key) { return @{ exists=$false; nodes=@() } }
  $nodes = [System.Collections.Generic.List[object]]::new()
  function Visit($node, $relative) {
    $values = @(foreach ($name in ($node.GetValueNames() | Sort-Object)) {
      @{
        name=$name
        kind=$node.GetValueKind($name).ToString()
        value=$node.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
      }
    })
    $nodes.Add(@{ relative=$relative; values=$values })
    foreach ($name in ($node.GetSubKeyNames() | Sort-Object)) {
      $child = $node.OpenSubKey($name, $false)
      try {
        Visit $child $(if ($relative) { $relative + '\' + $name } else { $name })
      } finally { $child.Dispose() }
    }
  }
  try { Visit $key '' } finally { $key.Dispose() }
  return @{ exists=$true; nodes=@($nodes.ToArray()) }
}
function Canonical($value) { return ($value | ConvertTo-Json -Depth 20 -Compress) }
function Equivalent($left, $right) {
  if ($left.exists -ne $right.exists -or $left.nodes.Count -ne $right.nodes.Count) { return $false }
  for ($i=0; $i -lt $left.nodes.Count; $i++) {
    $a=$left.nodes[$i]; $b=$right.nodes[$i]
    if ($a.relative -ne $b.relative -or $a.values.Count -ne $b.values.Count) { return $false }
    for ($j=0; $j -lt $a.values.Count; $j++) {
      $x=$a.values[$j]; $y=$b.values[$j]
      if ($x.name -ne $y.name -or $x.kind -ne $y.kind -or (Canonical $x.value) -cne (Canonical $y.value)) {
        return $false
      }
    }
  }
  return $true
}
$current = Read-Association
if ($Action -eq 'snapshot') {
  if (Test-Path -LiteralPath $snapshotPath) { throw 'Refuse overwriting protocol snapshot' }
  [System.IO.File]::WriteAllText($snapshotPath, (Canonical $current), [System.Text.UTF8Encoding]::new($false))
  Write-Output '{"captured":true}'
  exit 0
}
$before = Get-Content -LiteralPath $snapshotPath -Raw | ConvertFrom-Json -AsHashtable
$changed = -not (Equivalent $current $before)
if ($changed -and $Action -eq 'check') { throw 'Protocol association differs from original snapshot' }
if ($changed) {
  $commandNode = @($current.nodes | Where-Object { $_.relative -eq 'shell\open\command' })
  $commandValue = @($commandNode.values | Where-Object { $_.name -eq '' })
  if ($commandValue.Count -ne 1 -or $commandValue[0].value -isnot [string]) {
    throw 'Refuse restore: unexpected current protocol command'
  }
  if ($commandValue[0].value -notmatch '^"([^"]+)"(?:\s|$)') {
    throw 'Refuse restore: current command is not a quoted known test executable'
  }
  if ($knownExecutables -notcontains [System.IO.Path]::GetFullPath($Matches[1])) {
    throw 'Refuse restore: another application changed the association'
  }
  # Only the exact protocol key changed by these packaged fixtures is restored.
  [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($keyPath, $false)
  if ($before.exists) {
    foreach ($node in $before.nodes) {
      $path = if ($node.relative) { $keyPath + '\' + $node.relative } else { $keyPath }
      $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($path, $true)
      try {
        foreach ($entry in $node.values) {
          $kind = [System.Enum]::Parse([Microsoft.Win32.RegistryValueKind], $entry.kind)
          $value = $entry.value
          switch ($entry.kind) {
            'Binary' { $value=[byte[]]$entry.value; break }
            'DWord' { $value=[int]$entry.value; break }
            'QWord' { $value=[long]$entry.value; break }
            'MultiString' { $value=[string[]]$entry.value; break }
            default { $value=[string]$entry.value }
          }
          $key.SetValue([string]$entry.name, $value, $kind)
        }
      } finally { $key.Dispose() }
    }
  }
}
$after = Read-Association
if (-not (Equivalent $after $before)) { throw 'Protocol restoration verification failed' }
[System.IO.File]::WriteAllText(
  (Join-Path $EvidenceDirectory 'protocol-association-after.json'),
  (Canonical $after), [System.Text.UTF8Encoding]::new($false)
)
Write-Output (Canonical @{ restored=$changed; matchesOriginal=$true })
