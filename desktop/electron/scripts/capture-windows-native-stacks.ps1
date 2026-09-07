<#
.SYNOPSIS
Capture bounded native stack metadata for an explicitly owned Windows process.
.DESCRIPTION
The caller supplies the PID and UTC StartTime ticks recorded at process launch.
Keep the target Process.Handle open for the entire capture. Only an existing x64
SDK CDB is used; this script never installs tools or downloads symbols.

CDB runs noninvasively without suspending the target (-pvr). Its only commands
are fixed markers, selected-thread knL backtraces, and qd. Raw debugger output
stays in bounded memory and is never logged. Only validated module, symbol and
offset columns become JSONL. No parameters, registers, stack bytes or dumps are
requested. Unsuspended stack unwinding is best-effort, not a coherent snapshot.

CDB has a 3500 ms internal deadline. The caller must additionally bound this
PowerShell helper to 5000 ms and reap its own helper process tree if necessary;
terminating only PowerShell could leave CDB behind. The target is not a child of
the debugger. This script only terminates the exact CDB Process it creates.

References:
https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/cdb-command-line-options
https://learn.microsoft.com/en-us/windows-hardware/drivers/debuggercmds/thread-syntax
https://learn.microsoft.com/en-us/windows-hardware/drivers/debuggercmds/k--kb--kc--kd--kp--kp--kv--display-stack-backtrace-
https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/ending-a-debugging-session-in-cdb
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [int]$TargetPid,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedStartTicks
)

$ErrorActionPreference = 'Stop'
$diagnosticClock = [System.Diagnostics.Stopwatch]::StartNew()
$maxOutputBytes = 256 * 1024
$maxOutputRecords = 2200
$state = @{
    status = 'helper-error'
    outputBytes = 0
    outputRecords = 0
    outputLimited = $false
    stdoutBytes = 0
    stderrBytes = 0
    discardedLines = 0
    frameCount = 0
    frameThreads = @{}
    seenFrames = @{}
    selectedTids = @{}
    threadsTruncated = $false
    currentTid = 0
    pendingLine = ''
    commandComplete = $false
    cdbPid = $null
    cdbExitCode = $null
    cdbExitObserved = $false
}

function Write-NativeRecord {
    param([hashtable]$Record, [switch]$Terminal)

    $json = $Record | ConvertTo-Json -Compress -Depth 4
    $size = [System.Text.Encoding]::UTF8.GetByteCount($json + [Console]::Out.NewLine)
    # Reserve enough space and one record for a bounded completion summary.
    $byteLimit = if ($Terminal) { $maxOutputBytes } else { $maxOutputBytes - 2048 }
    $recordLimit = if ($Terminal) { $maxOutputRecords } else { $maxOutputRecords - 1 }
    if ($state.outputBytes + $size -gt $byteLimit -or $state.outputRecords -ge $recordLimit) {
        $state.outputLimited = $true
        $state.status = 'output-limit'
        return $false
    }
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
    $state.outputBytes += $size
    $state.outputRecords += 1
    return $true
}

function Write-NativePhase {
    param(
        [ValidateSet('script-start', 'identity-verified', 'tool-verified', 'threads-enumerated', 'cdb-started', 'cdb-exited', 'complete')]
        [string]$Phase
    )

    $record = @{
        kind = 'phase'
        phase = $Phase
        pid = $TargetPid
        elapsedMs = $diagnosticClock.ElapsedMilliseconds
    }
    if ($null -ne $state.cdbPid) { $record.cdbPid = $state.cdbPid }
    [void](Write-NativeRecord $record)
}

function Set-TargetFailure {
    param(
        [ValidateSet('invalid-identity', 'not-found', 'identity-mismatch', 'target-exited', 'identity-changed')]
        [string]$Status
    )

    $state.status = $Status
    [void](Write-NativeRecord @{ kind = 'target'; pid = $TargetPid; status = $Status })
}

function Read-NativeLine {
    param([string]$Line)

    if ($Line.Length -gt 4096) {
        $state.discardedLines += 1
        return
    }
    if ($Line -cmatch '^OPENSQUILLA_NATIVE_THREAD_([0-9]{1,10})$') {
        $threadId = [long]$Matches[1]
        $state.currentTid = if ($state.selectedTids.ContainsKey($threadId)) { $threadId } else { 0 }
        return
    }
    if ($Line -ceq 'OPENSQUILLA_NATIVE_COMPLETE') {
        $state.currentTid = 0
        $state.commandComplete = $true
        return
    }

    # knL: frame index, Child-SP, RetAddr, Call Site. Addresses are matched only
    # to recognize the table; they are never serialized. Reject every other line.
    $framePattern = '^\s*([0-9a-fA-F]{1,2})\s+[0-9a-fA-F`]{8,17}\s+[0-9a-fA-F`]{8,17}\s+(.+?)\s*$'
    if ($state.currentTid -eq 0 -or $Line -cnotmatch $framePattern) {
        $state.discardedLines += 1
        return
    }
    $index = [Convert]::ToInt32($Matches[1], 16)
    $callSite = $Matches[2]
    $frameKey = [string]$state.currentTid + ':' + $index
    if ($index -ge 32 -or $state.seenFrames.ContainsKey($frameKey)) {
        $state.discardedLines += 1
        return
    }

    $offset = $null
    if ($callSite -cmatch '\+0x([0-9a-fA-F]{1,16})$') {
        $offset = '0x' + $Matches[1].ToLowerInvariant()
        $callSite = $callSite.Substring(0, $callSite.Length - $Matches[0].Length)
    }
    $parts = $callSite.Split('!', 2)
    $module = $parts[0]
    if ($module -cnotmatch '^[A-Za-z0-9_.-]{1,128}$') {
        $state.discardedLines += 1
        return
    }
    $symbol = if ($parts.Length -eq 2) { $parts[1] } else { $null }
    # Do not admit whitespace, argument parentheses, quotes, source paths or
    # arbitrary debugger prose through a permissive symbol-name expression.
    if (($null -ne $symbol -and $symbol -cnotmatch '^[A-Za-z0-9_?$@:.<>~`-]{1,256}$') -or
        ($null -eq $symbol -and $null -eq $offset)) {
        $state.discardedLines += 1
        return
    }
    $record = @{
        kind = 'frame'
        pid = $TargetPid
        tid = $state.currentTid
        index = $index
        module = $module
    }
    if ($null -ne $symbol) { $record.symbol = $symbol }
    if ($null -ne $offset) { $record.offset = $offset }
    if (Write-NativeRecord $record) {
        $state.frameCount += 1
        $state.frameThreads[$state.currentTid] = $true
        $state.seenFrames[$frameKey] = $true
    }
}

function Read-NativeBytes {
    param([byte[]]$Buffer, [int]$Count, [bool]$IsErrorStream)

    if ($IsErrorStream) { $state.stderrBytes += $Count } else { $state.stdoutBytes += $Count }
    if ($state.stdoutBytes + $state.stderrBytes -gt $maxOutputBytes) {
        $state.outputLimited = $true
        $state.status = 'output-limit'
        return
    }
    if ($IsErrorStream) { return }

    # Accepted metadata is ASCII. Other text is discarded by the strict parser;
    # this buffer is bounded by the total raw output limit and never leaves RAM.
    $state.pendingLine += [System.Text.Encoding]::ASCII.GetString($Buffer, 0, $Count)
    while (($lineEnd = $state.pendingLine.IndexOf("`n")) -ge 0) {
        $line = $state.pendingLine.Substring(0, $lineEnd).TrimEnd("`r")
        $state.pendingLine = $state.pendingLine.Substring($lineEnd + 1)
        Read-NativeLine $line
        if ($state.outputLimited) { return }
    }
}

function Invoke-NativeCapture {
    $ownedTarget = $null
    $debugger = $null
    $workDirectory = $null
    $symbolDirectory = $null
    try {
        Write-NativePhase 'script-start'
        if ($TargetPid -le 0 -or $TargetPid -eq $PID -or $ExpectedStartTicks -cnotmatch '^[0-9]{15,20}$') {
            Set-TargetFailure 'invalid-identity'
            return
        }
        try {
            $ownedTarget = [System.Diagnostics.Process]::GetProcessById($TargetPid)
            # Keep this exact process object referenced while CDB uses its PID.
            $null = $ownedTarget.Handle
        }
        catch {
            Set-TargetFailure 'not-found'
            return
        }
        if ($ownedTarget.StartTime.ToUniversalTime().Ticks.ToString() -ne $ExpectedStartTicks) {
            Set-TargetFailure 'identity-mismatch'
            return
        }
        Write-NativePhase 'identity-verified'

        $sdkRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
        if ([string]::IsNullOrWhiteSpace($sdkRoot)) {
            $state.status = 'unavailable'
            return
        }
        $debuggerPath = Join-Path $sdkRoot 'Windows Kits\10\Debuggers\x64\cdb.exe'
        if (-not [System.IO.File]::Exists($debuggerPath)) {
            $state.status = 'unavailable'
            return
        }
        $versionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($debuggerPath)
        $version = '{0}.{1}.{2}.{3}' -f $versionInfo.FileMajorPart, $versionInfo.FileMinorPart,
            $versionInfo.FileBuildPart, $versionInfo.FilePrivatePart
        $toolHash = (Get-FileHash -LiteralPath $debuggerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        [void](Write-NativeRecord @{ kind = 'tool'; name = 'cdb'; version = $version; sha256 = $toolHash })
        Write-NativePhase 'tool-verified'

        $ownedTarget.Refresh()
        if ($ownedTarget.HasExited) {
            Set-TargetFailure 'target-exited'
            return
        }
        $threadIds = @($ownedTarget.Threads | ForEach-Object { [long]$_.Id })
        $state.threadsTruncated = $threadIds.Count -gt 64
        $commands = [System.Collections.Generic.List[string]]::new()
        foreach ($threadId in ($threadIds | Select-Object -First 64)) {
            $state.selectedTids[$threadId] = $true
            $commands.Add('.echo OPENSQUILLA_NATIVE_THREAD_' + $threadId)
            $commands.Add('~~[0x' + $threadId.ToString('x') + '] knL 0x20')
        }
        $commands.Add('.echo OPENSQUILLA_NATIVE_COMPLETE')
        $commands.Add('qd')
        Write-NativePhase 'threads-enumerated'

        # An empty dedicated working directory prevents an inherited ntsd.ini.
        # The symbol directory is local and empty; no symbol server is enabled.
        $workDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ('opensquilla-native-stacks-' + [Guid]::NewGuid().ToString('N'))
        $symbolDirectory = Join-Path $workDirectory 'symbols'
        [void][System.IO.Directory]::CreateDirectory($symbolDirectory)
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $debuggerPath
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.WorkingDirectory = $workDirectory
        $startInfo.RedirectStandardInput = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.Environment.Clear()
        foreach ($name in @('SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA')) {
            $value = [Environment]::GetEnvironmentVariable($name)
            if ($null -ne $value) { $startInfo.Environment[$name] = $value }
        }
        # SDK CDB 10.0.26100.8249 accepts -netsyms:no; its help misspells it.
        foreach ($argument in @('-pvr', '-pd', '-noshell', '-nosqm', '-sins', '-ses', '-netsyms:no',
            '-y', $symbolDirectory, '-p', $TargetPid.ToString(), '-c', ($commands -join ';'))) {
            $startInfo.ArgumentList.Add($argument)
        }
        if ($ownedTarget.HasExited) {
            Set-TargetFailure 'target-exited'
            return
        }
        $debugger = [System.Diagnostics.Process]::new()
        $debugger.StartInfo = $startInfo
        if (-not $debugger.Start()) { return }
        $state.cdbPid = $debugger.Id
        # All commands are fixed at launch; never forward caller input. Keep
        # this pipe open until qd or the helper deadline closes the debugger:
        # an early EOF can end an interactive debugger before startup commands.
        $debuggerClock = [System.Diagnostics.Stopwatch]::StartNew()
        Write-NativePhase 'cdb-started'

        $outBuffer = [byte[]]::new(4096)
        $errBuffer = [byte[]]::new(4096)
        $outTask = $debugger.StandardOutput.BaseStream.ReadAsync($outBuffer, 0, $outBuffer.Length)
        $errTask = $debugger.StandardError.BaseStream.ReadAsync($errBuffer, 0, $errBuffer.Length)
        $outEnded = $false
        $errEnded = $false
        while ($true) {
            if ($ownedTarget.HasExited) {
                Set-TargetFailure 'target-exited'
                return
            }
            foreach ($isErrorStream in @($false, $true)) {
                $readTask = if ($isErrorStream) { $errTask } else { $outTask }
                $ended = if ($isErrorStream) { $errEnded } else { $outEnded }
                if (-not $ended -and $readTask.IsCompleted) {
                    $count = $readTask.GetAwaiter().GetResult()
                    $buffer = if ($isErrorStream) { $errBuffer } else { $outBuffer }
                    Read-NativeBytes $buffer $count $isErrorStream
                    if ($state.outputLimited) { return }
                    if ($isErrorStream) {
                        $errEnded = $count -eq 0
                        if (-not $errEnded) { $errTask = $debugger.StandardError.BaseStream.ReadAsync($errBuffer, 0, $errBuffer.Length) }
                    }
                    else {
                        $outEnded = $count -eq 0
                        if (-not $outEnded) { $outTask = $debugger.StandardOutput.BaseStream.ReadAsync($outBuffer, 0, $outBuffer.Length) }
                    }
                }
            }
            if ($debugger.HasExited -and $outEnded -and $errEnded) { break }
            if ($debuggerClock.ElapsedMilliseconds -ge 3500) {
                $state.status = 'timeout'
                return
            }
            Start-Sleep -Milliseconds 10
        }
        $state.cdbExitObserved = $true
        $state.cdbExitCode = $debugger.ExitCode
        Write-NativePhase 'cdb-exited'
        $ownedTarget.Refresh()
        if ($ownedTarget.HasExited) {
            Set-TargetFailure 'target-exited'
            return
        }
        if ($ownedTarget.StartTime.ToUniversalTime().Ticks.ToString() -ne $ExpectedStartTicks) {
            Set-TargetFailure 'identity-changed'
            return
        }
        if ($state.cdbExitCode -ne 0 -or -not $state.commandComplete) { $state.status = 'cdb-error' }
        elseif ($state.frameCount -eq 0) { $state.status = 'no-frames' }
        else { $state.status = 'complete' }
    }
    catch {
        # Exception messages can contain paths, arguments or debugger output.
        # Keep only a fixed status, never ErrorRecord or exception text.
        if (-not $state.outputLimited) { $state.status = 'helper-error' }
    }
    finally {
        if ($null -ne $debugger) {
            try {
                if (-not $debugger.HasExited) { $debugger.Kill() }
                $state.cdbExitObserved = $debugger.WaitForExit(250)
                if ($state.cdbExitObserved) { $state.cdbExitCode = $debugger.ExitCode }
            }
            catch { $state.cdbExitObserved = $false }
            $debugger.Dispose()
        }
        if ($null -ne $ownedTarget) { $ownedTarget.Dispose() }
        $state.pendingLine = ''
        # Delete only the exact, empty directories created here. Never recurse
        # through a computed path; unexpected contents are left untouched.
        if ($null -ne $symbolDirectory) {
            try { [System.IO.Directory]::Delete($symbolDirectory, $false) } catch {}
        }
        if ($null -ne $workDirectory) {
            try { [System.IO.Directory]::Delete($workDirectory, $false) } catch {}
        }
    }
}

Invoke-NativeCapture
Write-NativePhase 'complete'
[void](Write-NativeRecord @{
    kind = 'completion'
    pid = $TargetPid
    status = $state.status
    # Unique TIDs with emitted frames, not the total enumerated thread count.
    threadCount = $state.frameThreads.Count
    threadsTruncated = $state.threadsTruncated
    frameCount = $state.frameCount
    stdoutBytes = $state.stdoutBytes
    stderrBytes = $state.stderrBytes
    discardedLines = $state.discardedLines
    elapsedMs = $diagnosticClock.ElapsedMilliseconds
    cdbPid = $state.cdbPid
    cdbExitCode = $state.cdbExitCode
    cdbExitObserved = $state.cdbExitObserved
} -Terminal)
if ($state.status -in @('complete', 'unavailable')) { exit 0 }
exit 2
