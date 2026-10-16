param(
    [Parameter(Mandatory = $true)][string]$StopFile,
    [Parameter(Mandatory = $true)][string]$OutputFile
)

# One child process, one sample per second, bounded in memory. Never emit trace
# writes on a test's timed path. These are host counters, not proof of a
# particular SQLite operation's physical I/O or scheduling cause.
$ErrorActionPreference = 'Stop'
$samples = [System.Collections.Generic.List[object]]::new()
$diagnosticErrors = [System.Collections.Generic.List[string]]::new()
$originTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
$frequency = [System.Diagnostics.Stopwatch]::Frequency
$originEpochMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$counters = @(
    '\Processor(_Total)\% Processor Time',
    '\System\Processor Queue Length',
    '\Memory\Available MBytes',
    '\PhysicalDisk(_Total)\Current Disk Queue Length',
    '\PhysicalDisk(_Total)\Avg. Disk sec/Read',
    '\PhysicalDisk(_Total)\Avg. Disk sec/Write',
    '\PhysicalDisk(_Total)\Disk Read Bytes/sec',
    '\PhysicalDisk(_Total)\Disk Write Bytes/sec'
)
$stopReason = 'sample_limit'
try {
    while ($samples.Count -lt 3600) {
        if (Test-Path -LiteralPath $StopFile) {
            $stopReason = 'stop_file'
            break
        }
        $sampleStarted = [System.Diagnostics.Stopwatch]::GetTimestamp()
        $sample = Get-Counter -Counter $counters -SampleInterval 1 -MaxSamples 1 -ErrorAction Stop
        $ticks = [System.Diagnostics.Stopwatch]::GetTimestamp()
        $samples.Add([ordered]@{
            utc = $sample.Timestamp.ToUniversalTime().ToString('o')
            epoch_ms = ([DateTimeOffset]$sample.Timestamp).ToUnixTimeMilliseconds()
            observed_epoch_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            perf_counter_ticks = $ticks
            elapsed_ms = (($ticks - $originTicks) * 1000.0 / $frequency)
            values = @($sample.CounterSamples | ForEach-Object {
                [ordered]@{ path = $_.Path; value = $_.CookedValue; status = $_.Status }
            })
        })
        # A single-sample Get-Counter invocation can return immediately. Enforce
        # the cadence explicitly so the observer cannot become a busy loop.
        $sampleElapsedMs = ([System.Diagnostics.Stopwatch]::GetTimestamp() - $sampleStarted) * 1000.0 / $frequency
        $remainingMs = [Math]::Ceiling(1000.0 - $sampleElapsedMs)
        if ($remainingMs -gt 0) { Start-Sleep -Milliseconds ([int]$remainingMs) }
    }
} catch {
    $stopReason = 'counter_error'
    $diagnosticErrors.Add($_.Exception.GetType().Name + ': ' + $_.Exception.Message)
} finally {
    $payload = [ordered]@{
        schema_version = 1
        pid = $PID
        sample_interval_seconds = 1
        sample_limit = 3600
        origin_epoch_ms = $originEpochMs
        origin_perf_counter_ticks = $originTicks
        perf_counter_frequency = $frequency
        stopped_epoch_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        stop_reason = $stopReason
        diagnostic_errors = $diagnosticErrors.ToArray()
        samples = $samples.ToArray()
    }
    $payload | ConvertTo-Json -Depth 7 | Out-File -LiteralPath $OutputFile -Encoding utf8
}
