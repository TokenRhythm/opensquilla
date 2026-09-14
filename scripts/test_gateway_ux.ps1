param(
  [ValidateSet('Start', 'Stop')]
  [string]$Action = 'Start',
  [int]$GatewayPort = 8765,
  [int]$WebPort = 5173
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$root = Join-Path ([System.IO.Path]::GetTempPath()) 'opensquilla-gateway-ux'
$state = Join-Path $root 'state'
$userState = Join-Path $root 'user-state'
$logs = Join-Path $root 'logs'
$pidFile = Join-Path $root 'pids.json'

if ($Action -eq 'Stop') {
  if (Test-Path $pidFile) {
    $pids = Get-Content $pidFile | ConvertFrom-Json
    foreach ($id in @($pids.gateway, $pids.web)) {
      if ($id) { Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
  }
  Write-Host "Stopped isolated Gateway UX environment. Data remains at $root"
  exit 0
}

New-Item -ItemType Directory -Force -Path $state, $userState, $logs | Out-Null
$env:OPENSQUILLA_STATE_DIR = $state
$env:OPENSQUILLA_USER_STATE_DIR = $userState
$env:OPENSQUILLA_LISTEN = "127.0.0.1:$GatewayPort"

$uvCommand = (Get-Command uv -ErrorAction Stop).Source
$npmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
$gateway = Start-Process -FilePath $uvCommand -ArgumentList @('run', 'opensquilla', 'gateway', 'run', '--listen', "127.0.0.1:$GatewayPort") -WorkingDirectory $repo -RedirectStandardOutput (Join-Path $logs 'gateway.out.log') -RedirectStandardError (Join-Path $logs 'gateway.err.log') -PassThru
$web = Start-Process -FilePath $npmCommand -ArgumentList @('run', 'dev', '--', '--host', '127.0.0.1', '--port', "$WebPort") -WorkingDirectory (Join-Path $repo 'opensquilla-webui') -RedirectStandardOutput (Join-Path $logs 'web.out.log') -RedirectStandardError (Join-Path $logs 'web.err.log') -PassThru
@{ gateway = $gateway.Id; web = $web.Id; gatewayPort = $GatewayPort; webPort = $WebPort; root = $root } | ConvertTo-Json | Set-Content $pidFile

Write-Host "Gateway UX test environment started."
Write-Host "WebUI:    http://127.0.0.1:$WebPort"
Write-Host "Gateway:  ws://127.0.0.1:$GatewayPort"
Write-Host "Temp data: $root"
Write-Host "Stop:     powershell -ExecutionPolicy Bypass -File .\scripts\test_gateway_ux.ps1 Stop"
Write-Host "To simulate a disconnect, run: Stop-Process -Id $($gateway.Id) -Force"
