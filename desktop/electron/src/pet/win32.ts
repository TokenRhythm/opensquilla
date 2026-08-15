// Win32 bridge for the pet's Windows mischief mode.
//
// All P/Invoke lives in pet/win32/PetWin32.cs. It is compiled ONCE to a DLL in
// userData and loaded with `Add-Type -Path <dll>` — far faster than re-compiling
// inline C# on every call (the inline Add-Type compile is a multi-second
// bottleneck that broke patrol/click-through timing). Follows LLMPET's focus.js
// pattern: spawn a PowerShell process that does the work and returns JSON.

import { execFile } from 'node:child_process'
import { app } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

export interface Win32Window {
  hwnd: string
  pid: number
  title: string
  cls: string
  x: number
  y: number
  w: number
  h: number
  visible: boolean
}

export interface Win32Process {
  name: string
  pid: number
}

// Diagnostic sink — set by pet-backend when it wires up antics.
let logSink: ((msg: string) => void) | null = null
export function setWin32Log(fn: (msg: string) => void): void { logSink = fn }
function wlog(msg: string): void { try { logSink && logSink(msg) } catch {} }

// Pass the script via -EncodedCommand (UTF-16LE base64). This is the only
// robust way on Windows to avoid backslashes / unicode chars in file paths
// getting mangled by the command-line parser between Node's execFile and
// PowerShell's -Command argument tokenizer. The prior `-Command <script>`
// path silently ate backslashes for paths containing non-ASCII characters
// (e.g. "open桌宠"), which made ensureDll() reject and every subsequent
// win32() call short-circuit to null.
function encodeCommand(script: string): string {
  const buf = Buffer.from(script, 'utf16le')
  return buf.toString('base64')
}

function runPs(script: string, timeout = 8000): Promise<string> {
  return new Promise((resolve, reject) => {
    const args = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encodeCommand(script)]
    execFile('powershell.exe', args, { timeout, windowsHide: true }, (err, stdout, stderr) => {
      if (err) {
        wlog(`[win32] PS err: ${err.message}${stderr ? ` stderr=${String(stderr).slice(0, 200)}` : ''}`)
        reject(err)
        return
      }
      // PowerShell emits progress records to stderr as CLIXML by default; that's
      // not a real error. Only surface stderr that isn't just the CLIXML wrapper.
      const errText = String(stderr || '').trim()
      if (errText && !errText.startsWith('#< CLIXML')) {
        wlog(`[win32] PS stderr: ${errText.slice(0, 300)}`)
      }
      resolve(String(stdout || '').trim())
    })
  })
}

// ── DLL cache: compile PetWin32.cs once, load forever ───────────────────────
let dllPromise: Promise<string> | null = null

function ensureDll(): Promise<string> {
  if (dllPromise) return dllPromise
  const dll = path.join(app.getPath('userData'), 'PetWin32.dll')
  const safeDll = dll.replace(/'/g, "''")
  dllPromise = (async () => {
    if (fs.existsSync(dll)) {
      try {
        await runPs(`Add-Type -Path '${safeDll}'; 'ok'`, 5000)
        wlog(`[win32] using cached DLL: ${dll}`)
        return dll
      } catch (e) { wlog(`[win32] cached DLL load failed, recompiling: ${e}`) }
    }
    const cs = path.join(app.getAppPath(), 'pet', 'win32', 'PetWin32.cs')
    if (!fs.existsSync(cs)) throw new Error(`PetWin32.cs missing at ${cs}`)
    // Read the C# source in Node (handles unicode paths correctly) and inline
    // it as a here-string, so the PowerShell script never has to open a file
    // by a possibly-mangled path.
    const csSource = fs.readFileSync(cs, 'utf8')
    const script = `$src = @'\n${csSource}\n'@\nAdd-Type -TypeDefinition $src -OutputAssembly '${safeDll}' -OutputType Library\n'compiled'`
    wlog(`[win32] compiling PetWin32.dll to ${dll}`)
    const out = await runPs(script, 30000)
    wlog(`[win32] compile output: ${out}`)
    if (!fs.existsSync(dll)) throw new Error(`PetWin32.dll compile failed (script len=${script.length}, out=${out})`)
    return dll
  })()
  dllPromise.catch((e) => { wlog(`[win32] ensureDll rejected: ${e}`); })
  return dllPromise
}

async function win32(psBody: string, timeout = 8000): Promise<string> {
  const dll = await ensureDll()
  const script = `Add-Type -Path '${dll.replace(/'/g, "''")}'; [PetWin32]::SetProcessDPIAware() | Out-Null; ${psBody}`
  return runPs(script, timeout)
}

// ── API ──────────────────────────────────────────────────────────────────────

export async function setCursorPos(x: number, y: number): Promise<boolean> {
  try {
    const out = await win32(`[PetWin32]::SetCursorPos(${Math.round(x)}, ${Math.round(y)})`, 5000)
    return out === 'True'
  } catch { return false }
}

export async function getCursorPos(): Promise<{ x: number; y: number } | null> {
  try {
    const out = await win32(
      '$p = New-Object PetWin32+POINT; [PetWin32]::GetCursorPos([ref]$p) | Out-Null; "$($p.X),$($p.Y)"',
      5000,
    )
    const m = /^(-?\d+),(-?\d+)$/.exec(out)
    return m ? { x: Number(m[1]), y: Number(m[2]) } : null
  } catch { return null }
}

const MOUSE_LEFTDOWN = 0x2
const MOUSE_LEFTUP = 0x4

export async function mouseClickAt(x: number, y: number): Promise<void> {
  try {
    await win32(
      `[PetWin32]::SetCursorPos(${Math.round(x)}, ${Math.round(y)}); ` +
      `[PetWin32]::mouse_event(${MOUSE_LEFTDOWN}, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 30; ` +
      `[PetWin32]::mouse_event(${MOUSE_LEFTUP}, 0, 0, 0, [UIntPtr]::Zero)`,
      5000,
    )
  } catch {}
}

/**
 * Full mouse drag from (x1,y1) to (x2,y2) inside ONE PowerShell process
 * (interpolated over `steps`, ~16ms apart). Used for pushing rival pet windows
 * to the screen edge and for cursor-stealing drags.
 */
export async function mouseDrag(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  steps = 24,
  stepMs = 16,
): Promise<void> {
  const st = Math.max(4, Math.min(200, Math.round(steps)))
  const ms = Math.max(4, Math.min(60, Math.round(stepMs)))
  try {
    await win32(
      `$x1=${Math.round(x1)}; $y1=${Math.round(y1)}; $x2=${Math.round(x2)}; $y2=${Math.round(y2)}; $n=${st}; ` +
      `[PetWin32]::SetCursorPos($x1, $y1); Start-Sleep -Milliseconds 20; ` +
      `[PetWin32]::mouse_event(${MOUSE_LEFTDOWN}, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 20; ` +
      `for ($i = 1; $i -le $n; $i++) { $t = $i / $n; ` +
      `[PetWin32]::SetCursorPos([int]($x1 + ($x2 - $x1) * $t), [int]($y1 + ($y2 - $y1) * $t)); ` +
      `Start-Sleep -Milliseconds ${ms} } ` +
      `[PetWin32]::mouse_event(${MOUSE_LEFTUP}, 0, 0, 0, [UIntPtr]::Zero)`,
      20000,
    )
  } catch {}
}

/** Snatch the cursor: keep pulling it toward (tx,ty) for `pulls` iterations. */
export async function cursorPull(tx: number, ty: number, pulls = 8, stepMs = 24): Promise<void> {
  const n = Math.max(2, Math.min(40, Math.round(pulls)))
  const ms = Math.max(8, Math.min(80, Math.round(stepMs)))
  try {
    await win32(
      `$tx=${Math.round(tx)}; $ty=${Math.round(ty)}; ` +
      `for ($i = 0; $i -lt ${n}; $i++) { $p = New-Object PetWin32+POINT; [PetWin32]::GetCursorPos([ref]$p) | Out-Null; ` +
      `$dx = $tx - $p.X; $dy = $ty - $p.Y; ` +
      `[PetWin32]::SetCursorPos($p.X + [int]($dx * 0.45), $p.Y + [int]($dy * 0.45)); ` +
      `Start-Sleep -Milliseconds ${ms} }`,
      10000,
    )
  } catch {}
}

// SetWindowPos flags
const SWP_NOSIZE = 0x0001
const SWP_NOMOVE = 0x0002
const SWP_NOACTIVATE = 0x0010
const HWND_TOPMOST = -1
const HWND_NOTOPMOST = -2

// PowerShell can't cast a decimal string directly to [IntPtr]; go through
// [int64] first. Every helper that takes an hwnd builds the pointer expression
// this way.
function hwndExpr(hwnd: string): string {
  const clean = String(hwnd).replace(/[^0-9-]/g, '') || '0'
  return `[IntPtr]([int64]${clean})`
}

export async function setTopmost(hwnd: string, on: boolean): Promise<void> {
  try {
    await win32(
      `[PetWin32]::SetWindowPos(${hwndExpr(hwnd)}, ` +
      `[IntPtr](${on ? HWND_TOPMOST : HWND_NOTOPMOST}), 0, 0, 0, 0, ` +
      `${SWP_NOSIZE}|${SWP_NOMOVE}|${SWP_NOACTIVATE})`,
      5000,
    )
  } catch {}
}

export async function moveWindow(hwnd: string, x: number, y: number, w: number, h: number): Promise<void> {
  try {
    await win32(
      `[PetWin32]::MoveWindow(${hwndExpr(hwnd)}, ${Math.round(x)}, ${Math.round(y)}, ` +
      `${Math.max(1, Math.round(w))}, ${Math.max(1, Math.round(h))}, $true)`,
      5000,
    )
  } catch {}
}

export async function getWindowRect(hwnd: string): Promise<{ x: number; y: number; w: number; h: number } | null> {
  try {
    const out = await win32(
      `$r = New-Object PetWin32+RECT; if ([PetWin32]::GetWindowRect(${hwndExpr(hwnd)}, [ref]$r)) ` +
      `{ "$($r.Left),$($r.Top),$($r.Right-$r.Left),$($r.Bottom-$r.Top)" } else { "" }`,
      5000,
    )
    const m = /^(-?\d+),(-?\d+),(\d+),(\d+)$/.exec(out)
    return m ? { x: Number(m[1]), y: Number(m[2]), w: Number(m[3]), h: Number(m[4]) } : null
  } catch { return null }
}

export async function postClose(hwnd: string): Promise<void> {
  try {
    await win32(`[PetWin32]::PostMessage(${hwndExpr(hwnd)}, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)`, 5000)
  } catch {}
}

export async function getForegroundWindow(): Promise<string | null> {
  try {
    const out = await win32('([PetWin32]::GetForegroundWindow()).ToInt64().ToString()', 5000)
    const v = out && /^-?[0-9]+$/.test(out) && out !== '0' ? out : null
    return v
  } catch { return null }
}

/**
 * Enumerate visible top-level windows. Returns JSON rows with hwnd/pid/title/
 * cls/rect. Used to find rival desktop-pet windows (ChatGPT/Codex mascot).
 */
export async function enumTopWindows(): Promise<Win32Window[]> {
  try {
    const script =
      `$rows = New-Object System.Collections.ArrayList; ` +
      `$cb = [PetWin32+EnumWindowsProc]{ param($h, $l) ` +
      `if ([PetWin32]::IsWindowVisible($h)) { ` +
      `$pid2 = [uint32]0; [PetWin32]::GetWindowThreadProcessId($h, [ref]$pid2) | Out-Null; ` +
      `$t = New-Object System.Text.StringBuilder 512; [PetWin32]::GetWindowText($h, $t, 512) | Out-Null; ` +
      `$c = New-Object System.Text.StringBuilder 256; [PetWin32]::GetClassName($h, $c, 256) | Out-Null; ` +
      `$r = New-Object PetWin32+RECT; if ([PetWin32]::GetWindowRect($h, [ref]$r)) { ` +
      `$null = $rows.Add([pscustomobject]@{ hwnd=$h.ToString(); pid=[int]$pid2; title=$t.ToString(); cls=$c.ToString(); ` +
      `x=$r.Left; y=$r.Top; w=$r.Right-$r.Left; h=$r.Bottom-$r.Top }) } }; return $true }; ` +
      `[PetWin32]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null; ` +
      `$rows | ConvertTo-Json -Compress`
    const out = await win32(script, 8000)
    if (!out || out === '[]') return []
    const parsed = JSON.parse(out)
    return Array.isArray(parsed) ? parsed : [parsed]
  } catch {
    return []
  }
}

/** Snapshot of running process names + pids (for rival-pet presence checks). */
export async function processList(): Promise<Win32Process[]> {
  try {
    const out = await runPs(
      'Get-Process | Where-Object { $_.MainWindowTitle -or $true } | Select-Object -First 400 Name,Id | ConvertTo-Json -Compress',
      8000,
    )
    if (!out || out === '[]') return []
    const parsed = JSON.parse(out)
    const arr = Array.isArray(parsed) ? parsed : [parsed]
    return arr.filter((p: any) => p && p.Name != null && p.Id != null).map((p: any) => ({ name: String(p.Name), pid: Number(p.Id) }))
  } catch {
    return []
  }
}
