import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const WAIT_CHAIN_HELPER = fileURLToPath(new URL('./capture-windows-wait-chain.ps1', import.meta.url))
const MAX_OUTPUT_BYTES = 64 * 1024
const TARGET_STATUSES = new Set([
  'not-found', 'identity-mismatch', 'thread-not-in-target', 'exited-during-query',
  'identity-changed-during-query', 'thread-identity-changed',
])

function isPid(value) {
  return Number.isSafeInteger(value) && value > 0 && value <= 0xffffffff
}

// This only launches a diagnostic helper. A timeout can terminate that exact
// ChildProcess handle; it never signals the Electron target or a guessed PID.
async function runHelper(args, timeoutMs) {
  return new Promise(resolve => {
    const env = {}
    for (const name of ['SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PSModulePath']) {
      if (process.env[name] !== undefined) env[name] = process.env[name]
    }
    const child = spawn('pwsh.exe', ['-NoProfile', '-NonInteractive', ...args], {
      windowsHide: true, env, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stdoutBytes = 0
    let stderrBytes = 0
    let status = null
    let finished = false
    let exitObserved = false
    let containmentTimer
    const finish = () => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      clearTimeout(containmentTimer)
      child.stdout.destroy()
      child.stderr.destroy()
      child.unref()
      resolve({
        status: status || (child.exitCode === 0 ? 'complete' : 'exit-error'),
        helperPid: child.pid ?? null,
        helperExitCode: child.exitCode,
        helperSignal: child.signalCode,
        helperExitObserved: exitObserved,
        stdoutBytes, stderrBytes,
        // Callers must validate and reconstruct this; never emit raw output.
        stdout,
      })
    }
    const stop = reason => {
      if (status || finished) return
      status = reason
      try { child.kill('SIGKILL') } catch {}
      containmentTimer = setTimeout(finish, 500)
    }
    const timer = setTimeout(() => stop('timeout'), timeoutMs)
    child.stdout.on('data', chunk => {
      stdoutBytes += chunk.length
      if (stdoutBytes + stderrBytes > MAX_OUTPUT_BYTES) stop('output-limit')
      else stdout += chunk.toString('utf8')
    })
    child.stderr.on('data', chunk => {
      stderrBytes += chunk.length
      if (stdoutBytes + stderrBytes > MAX_OUTPUT_BYTES) stop('output-limit')
    })
    child.once('error', () => { status = 'spawn-error'; finish() })
    child.once('exit', () => { exitObserved = true })
    child.once('close', finish)
  })
}

export async function captureWindowsProcessStart(pid, { timeoutMs = 2_000 } = {}) {
  if (process.platform !== 'win32') return { status: 'unsupported-platform' }
  if (!isPid(pid)) return { status: 'invalid-identity' }
  const script = `$ErrorActionPreference='Stop'; $targetProcess=Get-Process -Id ${pid}; `
    + '@{pid=$targetProcess.Id;startTicks=$targetProcess.StartTime.ToUniversalTime().Ticks.ToString()} | ConvertTo-Json -Compress'
  const { stdout, ...result } = await runHelper(['-Command', script], timeoutMs)
  if (result.status !== 'complete') return result
  try {
    const identity = JSON.parse(stdout)
    if (identity.pid !== pid || !/^\d{15,20}$/.test(identity.startTicks)) throw new Error('identity')
    return { status: 'complete', pid, startTicks: identity.startTicks }
  } catch {
    return { ...result, status: 'invalid-output' }
  }
}

function sanitizeRecord(record) {
  if (record?.kind === 'target' && isPid(record.pid) && TARGET_STATUSES.has(record.status)) {
    return {
      kind: 'target', pid: record.pid, status: record.status,
      ...(isPid(record.tid) ? { tid: record.tid } : {}),
    }
  }
  if (!isPid(record?.tid)) throw new Error('thread')
  if (Number.isSafeInteger(record.error)) return { tid: record.tid, error: record.error }
  if (typeof record.cycle !== 'boolean' || !Array.isArray(record.nodes) || record.nodes.length > 16) {
    throw new Error('chain')
  }
  return {
    tid: record.tid,
    cycle: record.cycle,
    nodes: record.nodes.map((node, index) => {
      if (!Number.isSafeInteger(node.type) || !Number.isSafeInteger(node.status)) throw new Error('node')
      if (node.type === 8) {
        // WCT can identify only the owning process of a terminal COM/ALPC
        // wait. Keep that documented TID 0 without relaxing the queried head.
        if (!isPid(node.pid) || !(isPid(node.tid) || (index > 0 && node.tid === 0))) {
          throw new Error('thread identity')
        }
        return { type: node.type, status: node.status, pid: node.pid, tid: node.tid }
      }
      return { type: node.type, status: node.status }
    }),
  }
}

export async function captureWindowsWaitChain(identity, {
  timeoutMs = 5_000,
  helperPath = WAIT_CHAIN_HELPER,
} = {}) {
  if (process.platform !== 'win32') return { status: 'unsupported-platform' }
  if (!isPid(identity?.electronPid) || !/^\d{15,20}$/.test(identity?.windowsStartTimeTicks || '')) {
    return { status: 'unavailable-identity' }
  }
  const { stdout, ...result } = await runHelper([
    '-File', helperPath,
    '-TargetPid', String(identity.electronPid),
    '-ExpectedStartTicks', identity.windowsStartTimeTicks,
  ], timeoutMs)
  if (!['complete', 'exit-error'].includes(result.status)) return result
  try {
    const lines = stdout.trim().split(/\r?\n/).filter(Boolean)
    if (!lines.length || lines.length > 65) throw new Error('record count')
    return { ...result, records: lines.map(line => sanitizeRecord(JSON.parse(line))) }
  } catch {
    return { ...result, status: 'invalid-output' }
  }
}
