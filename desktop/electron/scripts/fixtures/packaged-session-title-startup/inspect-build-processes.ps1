$ErrorActionPreference = 'Stop'

# Read-only observation of common build workers. Never print command lines,
# terminate processes, or infer that the rest of the host is idle.
$processes = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'")
$builders = @(
    foreach ($process in $processes) {
        $command = $process.CommandLine
        if (-not $command) { continue }
        $kind = if ($command -match '(?i)pyinstaller') {
            'pyinstaller'
        } elseif ($command -match '(?i)build-gateway\.mjs|build-webui-dist\.mjs') {
            'application-build'
        } elseif ($command -match '(?i)vue-tsc|vite(?:\.js|\.mjs)?["\s]+build\b|electron-builder') {
            'frontend-or-package-build'
        } else {
            $null
        }
        if ($kind) {
            [pscustomobject]@{
                pid = [int]$process.ProcessId
                parentPid = [int]$process.ParentProcessId
                name = $process.Name
                kind = $kind
                createdAt = $process.CreationDate.ToUniversalTime().ToString('o')
                worktreeIds = @([regex]::Matches($command, 'worktrees[\\/]([a-f0-9]{4})') |
                    ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
            }
        }
    }
)
[pscustomobject]@{
    observedAt = [DateTime]::UtcNow.ToString('o')
    clear = $builders.Count -eq 0
    builders = $builders
    uninspectableWorkerCount = @($processes | Where-Object { -not $_.CommandLine }).Count
} | ConvertTo-Json -Depth 5 -Compress
