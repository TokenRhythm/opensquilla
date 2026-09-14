param(
  [ValidateSet('Start', 'Stop', 'Disconnect')]
  [string]$Action = 'Start',
  [ValidateRange(1, 65535)]
  [int]$GatewayPort = 18791,
  [ValidateRange(1, 65535)]
  [int]$WebPort = 5173
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$root = Join-Path ([System.IO.Path]::GetTempPath()) 'opensquilla-gateway-ux'
$state = Join-Path $root 'state'
$userState = Join-Path $root 'user-state'
$logs = Join-Path $root 'logs'
$pidFile = Join-Path $root 'pids.json'

function Stop-UxProcessTree($record) {
  if (-not $record) { return }
  if (-not $record.id -or -not $record.startedAtTicks) {
    throw 'Cannot verify the saved process identity. Inspect the old environment manually.'
  }
  $process = Get-Process -Id ([int]$record.id) -ErrorAction SilentlyContinue
  if (-not $process) { return }
  if ($process.StartTime.ToUniversalTime().Ticks -ne [long]$record.startedAtTicks) {
    throw "PID $($record.id) belongs to a different process; refusing to stop it."
  }
  # uv and npm are launchers; terminating only their PID leaves servers alive.
  & taskkill.exe /PID $record.id /T /F | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Could not stop process tree $($record.id) (taskkill exit $LASTEXITCODE)."
  }
}

function Get-UxProcessRecord($process) {
  return @{ id = $process.Id; startedAtTicks = $process.StartTime.ToUniversalTime().Ticks }
}

if ($Action -ne 'Start') {
  if (Test-Path $pidFile) {
    $pids = Get-Content $pidFile | ConvertFrom-Json
    Stop-UxProcessTree $pids.gateway
    if ($Action -eq 'Stop') {
      Stop-UxProcessTree $pids.web
      Remove-Item $pidFile -Force
    }
  }
  Write-Host "$Action completed. Data remains at $root"
  return
}

if (Test-Path $pidFile) {
  throw 'An environment is already recorded. Run this script with Stop before starting again.'
}
$uvCommand = (Get-Command uv -ErrorAction Stop).Source
$npmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
$cmdCommand = (Get-Command $env:ComSpec -ErrorAction Stop).Source
Get-Command taskkill.exe -ErrorAction Stop | Out-Null
New-Item -ItemType Directory -Force -Path $state, $userState, $logs | Out-Null

$overrides = @{
  OPENSQUILLA_STATE_DIR = $state
  OPENSQUILLA_USER_STATE_DIR = $userState
  OPENSQUILLA_LISTEN = '127.0.0.1'
  OPENSQUILLA_GATEWAY_URL = "http://127.0.0.1:$GatewayPort"
}
$previous = @{}
$gatewayRecord = $null
$webRecord = $null
try {
  foreach ($name in $overrides.Keys) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $overrides[$name], 'Process')
  }
  $gateway = Start-Process -FilePath $uvCommand -ArgumentList @('run', 'opensquilla', 'gateway', 'run', '--listen', '127.0.0.1', '--port', "$GatewayPort") -WorkingDirectory $repo -RedirectStandardOutput (Join-Path $logs 'gateway.out.log') -RedirectStandardError (Join-Path $logs 'gateway.err.log') -PassThru
  $gatewayRecord = Get-UxProcessRecord $gateway
  # A .cmd file needs cmd.exe when Start-Process redirects its output. Keep the
  # executable quoted for installations under paths such as Program Files.
  $npmArgs = '""{0}" run dev -- --host 127.0.0.1 --port {1} --strictPort"' -f $npmCommand, $WebPort
  $web = Start-Process -FilePath $cmdCommand -ArgumentList @('/d', '/s', '/c', $npmArgs) -WorkingDirectory (Join-Path $repo 'opensquilla-webui') -RedirectStandardOutput (Join-Path $logs 'web.out.log') -RedirectStandardError (Join-Path $logs 'web.err.log') -PassThru
  $webRecord = Get-UxProcessRecord $web
  @{ gateway = $gatewayRecord; web = $webRecord; gatewayPort = $GatewayPort; webPort = $WebPort; root = $root } | ConvertTo-Json | Set-Content $pidFile
} catch {
  Stop-UxProcessTree $webRecord
  Stop-UxProcessTree $gatewayRecord
  throw
} finally {
  foreach ($name in $previous.Keys) {
    [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
  }
}

Write-Host "Gateway UX test environment started."
Write-Host "WebUI:    http://127.0.0.1:$WebPort/control/"
Write-Host "Gateway:  ws://127.0.0.1:$GatewayPort"
Write-Host "Temp data: $root"
Write-Host "Stop:     powershell -ExecutionPolicy Bypass -File .\scripts\test_gateway_ux.ps1 Stop"
Write-Host "Disconnect: powershell -ExecutionPolicy Bypass -File .\scripts\test_gateway_ux.ps1 Disconnect"
