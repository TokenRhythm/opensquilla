import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'

const NATIVE_STACK_HELPER = fileURLToPath(new URL('./capture-windows-native-stacks.ps1', import.meta.url))
const MAX_OUTPUT_BYTES = 256 * 1024
const MAX_RECORDS = 2_200
const MAX_FRAMES = 2_048
const PHASES = new Set([
  'script-start', 'identity-verified', 'tool-verified', 'threads-enumerated',
  'cdb-started', 'cdb-exited', 'complete',
])
const TARGET_STATUSES = new Set([
  'invalid-identity', 'not-found', 'identity-mismatch', 'target-exited', 'identity-changed',
])
const COMPLETION_STATUSES = new Set([
  'complete', 'unavailable', ...TARGET_STATUSES, 'timeout', 'output-limit',
  'cdb-error', 'no-frames', 'helper-error',
])

function isPid(value) {
  return Number.isSafeInteger(value) && value > 0 && value <= 0xffffffff
}

function isCount(value, max = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= 0 && value <= max
}

async function runHelper(args, timeoutMs) {
  return new Promise(resolve => {
    const env = {}
    for (const name of ['SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PSModulePath', 'ProgramFiles', 'ProgramFiles(x86)']) {
      if (process.env[name] !== undefined) env[name] = process.env[name]
    }
    let child
    try {
      child = spawn('pwsh.exe', ['-NoProfile', '-NonInteractive', ...args], {
        windowsHide: true, env, stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch {
      resolve({ status: 'spawn-error', stdout: '' })
      return
    }
    const chunks = []
    let stdoutBytes = 0
    let stderrBytes = 0
    let status = null
    let finished = false
    let helperExitObserved = false
    let helperCloseObserved = false
    let helperTreeReaped = null
    let containmentSettled = false
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
        helperExitObserved, helperCloseObserved, helperTreeReaped,
        containment: status === 'timeout' || status === 'output-limit'
          ? helperTreeReaped === true && helperExitObserved && helperCloseObserved ? 'complete' : 'failed'
          : 'not-needed',
        stdoutBytes, stderrBytes,
        // Kept only until validated JSONL is reconstructed below, never returned.
        stdout: Buffer.concat(chunks).toString('utf8'),
      })
    }
    const stop = reason => {
      if (status || finished) return
      status = reason
      // Absolute additional containment budget, including waiting for close.
      containmentTimer = setTimeout(finish, 2_000)
      helperTreeReaped = false
      if (!helperExitObserved && child.exitCode === null && isPid(child.pid)) {
        void terminateWindowsProcessTree({
          pid: child.pid,
          timeoutMs: 1_500,
          fallback: () => {
            if (!helperExitObserved && child.exitCode === null) child.kill('SIGKILL')
          },
        }).then(reaped => {
          helperTreeReaped = reaped
        }).finally(() => {
          containmentSettled = true
          if (helperCloseObserved) finish()
        })
      } else {
        // An exited wrapper does not establish that its descendants exited.
        // Never kill the diagnostic target or a PID guessed from helper output.
        containmentSettled = true
        if (helperCloseObserved) finish()
      }
    }
    const timer = setTimeout(() => stop('timeout'), timeoutMs)
    child.stdout.on('data', chunk => {
      const remaining = Math.max(0, MAX_OUTPUT_BYTES - stdoutBytes - stderrBytes)
      if (remaining > 0) chunks.push(chunk.subarray(0, remaining))
      stdoutBytes += chunk.length
      if (stdoutBytes + stderrBytes > MAX_OUTPUT_BYTES) stop('output-limit')
    })
    child.stderr.on('data', chunk => {
      stderrBytes += chunk.length
      if (stdoutBytes + stderrBytes > MAX_OUTPUT_BYTES) stop('output-limit')
    })
    child.once('error', () => { status = 'spawn-error'; finish() })
    child.once('exit', () => { helperExitObserved = true })
    child.once('close', () => {
      helperCloseObserved = true
      if (!status || containmentSettled || status === 'spawn-error') finish()
    })
  })
}

function sanitizeRecord(record, pid) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) throw new Error('record')
  if (record.kind === 'tool') {
    if (record.name !== 'cdb' || typeof record.version !== 'string'
      || !/^\d{1,6}(?:\.\d{1,6}){3}$/.test(record.version)
      || typeof record.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(record.sha256)) throw new Error('tool')
    return { kind: 'tool', name: 'cdb', version: record.version, sha256: record.sha256 }
  }
  if (record.pid !== pid) throw new Error('target identity')
  if (record.kind === 'phase') {
    if (!PHASES.has(record.phase) || !isCount(record.elapsedMs)
      || (record.cdbPid !== undefined && (!isPid(record.cdbPid) || record.cdbPid === pid))) throw new Error('phase')
    return { kind: 'phase', phase: record.phase, pid, elapsedMs: record.elapsedMs,
      ...(record.cdbPid === undefined ? {} : { cdbPid: record.cdbPid }) }
  }
  if (record.kind === 'target' && TARGET_STATUSES.has(record.status)) {
    return { kind: 'target', pid, status: record.status }
  }
  if (record.kind === 'frame') {
    if (!isPid(record.tid) || !isCount(record.index, 31)
      || typeof record.module !== 'string' || !/^[A-Za-z0-9_.-]{1,128}$/.test(record.module)
      || (record.symbol !== undefined && (typeof record.symbol !== 'string' || !/^[A-Za-z0-9_?$@:.<>~`-]{1,256}$/.test(record.symbol)))
      || (record.offset !== undefined && (typeof record.offset !== 'string' || !/^0x[a-f0-9]{1,16}$/.test(record.offset)))) throw new Error('frame')
    return { kind: 'frame', pid, tid: record.tid, index: record.index, module: record.module,
      ...(record.symbol === undefined ? {} : { symbol: record.symbol }),
      ...(record.offset === undefined ? {} : { offset: record.offset }) }
  }
  if (record.kind === 'completion') {
    if (!COMPLETION_STATUSES.has(record.status) || !isCount(record.threadCount, 64)
      || typeof record.threadsTruncated !== 'boolean' || !isCount(record.frameCount, MAX_FRAMES)
      || !isCount(record.stdoutBytes) || !isCount(record.stderrBytes)
      || !isCount(record.discardedLines) || !isCount(record.elapsedMs)
      || !(record.cdbPid === null || (isPid(record.cdbPid) && record.cdbPid !== pid))
      || !(record.cdbExitCode === null || Number.isSafeInteger(record.cdbExitCode))
      || typeof record.cdbExitObserved !== 'boolean') throw new Error('completion')
    return { kind: 'completion', pid, status: record.status,
      threadCount: record.threadCount, threadsTruncated: record.threadsTruncated,
      frameCount: record.frameCount, stdoutBytes: record.stdoutBytes, stderrBytes: record.stderrBytes,
      discardedLines: record.discardedLines, elapsedMs: record.elapsedMs,
      cdbPid: record.cdbPid, cdbExitCode: record.cdbExitCode, cdbExitObserved: record.cdbExitObserved }
  }
  throw new Error('kind')
}

function parseRecords(stdout, expectedPid) {
  const lines = stdout.split(/\r?\n/)
  // Only newline-terminated records are evidence, even if a partial last write
  // happens to contain syntactically valid JSON when the helper is interrupted.
  const tail = lines.pop()
  const completeLines = lines.filter(line => line.trim())
  const records = []
  const frames = new Set()
  const threads = new Set()
  let invalidLines = 0
  for (const line of completeLines.slice(0, MAX_RECORDS)) {
    try {
      const record = sanitizeRecord(JSON.parse(line), expectedPid)
      if (record.kind === 'frame') {
        const key = `${record.tid}:${record.index}`
        if (frames.has(key) || frames.size >= MAX_FRAMES || (!threads.has(record.tid) && threads.size >= 64)) throw new Error('frame limit')
        frames.add(key)
        threads.add(record.tid)
      }
      records.push(record)
    } catch { invalidLines += 1 }
  }
  const excessLines = Math.max(0, completeLines.length - MAX_RECORDS)
  const discardedTailLines = tail ? 1 : 0
  const completions = records.filter(record => record.kind === 'completion')
  let consistentCompletion = completions.length === 1 && records.at(-1) === completions[0]
  const completion = completions[0]
  if (consistentCompletion) {
    // The PS protocol counts only TIDs with emitted frames. Enumerated threads
    // without usable frames do not contribute to this sampled thread count.
    consistentCompletion = completion.frameCount === frames.size && completion.threadCount === threads.size
    if (completion.status === 'complete') {
      consistentCompletion &&= frames.size > 0 && completion.cdbPid !== null
        && completion.cdbExitCode === 0 && completion.cdbExitObserved
        && records.filter(record => record.kind === 'tool').length === 1
        && records.some(record => record.kind === 'phase' && record.phase === 'identity-verified')
        && !records.some(record => record.kind === 'target')
    }
  }
  return { records, completionStatus: consistentCompletion ? completion.status : null,
    outputParse: { status: excessLines ? 'record-limit' : invalidLines ? 'invalid-record'
      : discardedTailLines ? 'incomplete-tail' : consistentCompletion ? 'complete'
        : records.length ? 'missing-completion' : 'empty',
    invalidLines, discardedTailLines, excessLines } }
}

export async function captureWindowsNativeStacks(identity, {
  timeoutMs = 5_000,
  helperPath = NATIVE_STACK_HELPER,
} = {}) {
  if (process.platform !== 'win32') return { status: 'unsupported-platform' }
  if (!isPid(identity?.electronPid) || typeof identity?.windowsStartTimeTicks !== 'string'
    || !/^\d{15,20}$/.test(identity.windowsStartTimeTicks)) return { status: 'invalid-identity' }
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 5_000
    || typeof helperPath !== 'string' || !helperPath || /[\x00\r\n]/.test(helperPath)) return { status: 'invalid-options' }
  const { stdout, ...result } = await runHelper([
    '-File', helperPath,
    '-TargetPid', String(identity.electronPid),
    '-ExpectedStartTicks', identity.windowsStartTimeTicks,
  ], timeoutMs)
  const { completionStatus, ...parsed } = parseRecords(stdout, identity.electronPid)
  let status = result.status
  if (result.status === 'complete') {
    status = parsed.outputParse.status === 'complete' ? completionStatus : 'invalid-output'
  } else if (result.status === 'exit-error' && parsed.outputParse.status === 'complete'
    && completionStatus && !['complete', 'unavailable'].includes(completionStatus)) {
    // The PS helper exits 2 for a reported diagnostic failure. Keep that typed
    // failure without ever promoting a nonzero exit to successful capture.
    status = completionStatus
  }
  return { ...result, status, ...parsed }
}
