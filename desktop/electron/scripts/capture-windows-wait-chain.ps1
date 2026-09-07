<#
.SYNOPSIS
Capture wait-chain metadata for an explicitly selected, owned Windows process.
.DESCRIPTION
Run this script in a separate helper process behind a bounded caller deadline:
synchronous WCT calls cannot be cancelled in-process. The caller must supply the
owned process identity recorded at launch and must terminate only this helper if
the deadline expires.

Output is JSONL containing object type/status, thread PID/TID, cycle, and control
or API errors. Object names and raw native buffers are never read or serialized.
No privileges are enabled. Flags are zero, so another process can appear as a
terminal thread node but its wait chain is not followed. Unsupported waits may
produce only one node; that does not establish the absence of a hang.

References:
https://learn.microsoft.com/en-us/windows/win32/debug/wait-chain-traversal
https://learn.microsoft.com/en-us/windows/win32/api/wct/nf-wct-getthreadwaitchain
https://learn.microsoft.com/en-us/windows/win32/api/wct/ns-wct-waitchain_node_info
https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/wct.h
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [int]$TargetPid,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedStartTicks,

    [uint32]$TargetTid = 0
)

$ErrorActionPreference = 'Stop'

try {
    $ownedTarget = Get-Process -Id $TargetPid -ErrorAction Stop
}
catch {
    @{ kind = 'target'; pid = $TargetPid; status = 'not-found' } | ConvertTo-Json -Compress
    exit 2
}

if ($ownedTarget.StartTime.ToUniversalTime().Ticks.ToString() -ne $ExpectedStartTicks) {
    @{ kind = 'target'; pid = $TargetPid; status = 'identity-mismatch' } | ConvertTo-Json -Compress
    exit 3
}

$ownedTids = @($ownedTarget.Threads | ForEach-Object { [uint32]$_.Id })
if ($TargetTid -ne 0) {
    if ($ownedTids -notcontains $TargetTid) {
        @{
            kind = 'target'
            pid = $TargetPid
            tid = $TargetTid
            status = 'thread-not-in-target'
        } | ConvertTo-Json -Compress
        exit 4
    }
    $ownedTids = @($TargetTid)
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class WaitChainMetadata
{
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern IntPtr OpenThreadWaitChainSession(uint flags, IntPtr callback);

    [DllImport("advapi32.dll")]
    private static extern void CloseThreadWaitChainSession(IntPtr session);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetThreadWaitChain(
        IntPtr session,
        UIntPtr context,
        uint flags,
        uint tid,
        ref uint count,
        IntPtr nodes,
        out bool cycle);

    public static string Query(uint tid)
    {
        IntPtr session = OpenThreadWaitChainSession(0, IntPtr.Zero);
        if (session == IntPtr.Zero)
        {
            return "{\"tid\":" + tid + ",\"error\":" + Marshal.GetLastWin32Error() + "}";
        }

        // Microsoft wct.h: two DWORD enums followed by an 8-byte-aligned union
        // containing WCHAR[128], LARGE_INTEGER and BOOL. Never read the name.
        const int stride = 280;
        const uint maxNodes = 16;
        IntPtr buffer = Marshal.AllocHGlobal(stride * (int)maxNodes);
        try
        {
            uint count = maxNodes;
            bool cycle;
            // No out-of-process COM/critical-section or network expansion.
            bool ok = GetThreadWaitChain(session, UIntPtr.Zero, 0, tid, ref count, buffer, out cycle);
            if (!ok)
            {
                return "{\"tid\":" + tid + ",\"error\":" + Marshal.GetLastWin32Error() + "}";
            }

            string output = "{\"tid\":" + tid + ",\"cycle\":" + (cycle ? "true" : "false") + ",\"nodes\":[";
            for (uint n = 0; n < Math.Min(count, maxNodes); n++)
            {
                IntPtr node = IntPtr.Add(buffer, checked((int)n * stride));
                int type = Marshal.ReadInt32(node, 0);
                int status = Marshal.ReadInt32(node, 4);
                if (n > 0)
                {
                    output += ",";
                }
                output += "{\"type\":" + type + ",\"status\":" + status;
                // WctThreadType = 8. The other union branch is never read.
                if (type == 8)
                {
                    output += ",\"pid\":" + unchecked((uint)Marshal.ReadInt32(node, 8));
                    output += ",\"tid\":" + unchecked((uint)Marshal.ReadInt32(node, 12));
                }
                output += "}";
            }
            return output + "]}";
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
            CloseThreadWaitChainSession(session);
        }
    }
}
'@

foreach ($ownedTid in ($ownedTids | Select-Object -First 64)) {
    $metadataJson = [WaitChainMetadata]::Query($ownedTid)
    try {
        $currentTarget = Get-Process -Id $TargetPid -ErrorAction Stop
    }
    catch {
        @{ kind = 'target'; pid = $TargetPid; status = 'exited-during-query' } | ConvertTo-Json -Compress
        exit 5
    }
    if ($currentTarget.StartTime.ToUniversalTime().Ticks.ToString() -ne $ExpectedStartTicks) {
        @{ kind = 'target'; pid = $TargetPid; status = 'identity-changed-during-query' } | ConvertTo-Json -Compress
        exit 6
    }

    $metadata = $metadataJson | ConvertFrom-Json
    if ($metadata.nodes -and ($metadata.nodes[0].pid -ne $TargetPid -or $metadata.nodes[0].tid -ne $ownedTid)) {
        @{
            kind = 'target'
            pid = $TargetPid
            tid = $ownedTid
            status = 'thread-identity-changed'
        } | ConvertTo-Json -Compress
        exit 7
    }
    $metadataJson
}
