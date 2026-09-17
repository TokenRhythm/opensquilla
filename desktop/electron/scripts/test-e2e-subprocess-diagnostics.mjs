import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileWithDiagnostics } from './e2e-subprocess-diagnostics.mjs'

const options = { windowsHide: true, timeout: 20_000, killSignal: 'SIGKILL' }
const output = await execFileWithDiagnostics(process.execPath, ['-e',
  "process.stdout.write('stdout ✓'); process.stderr.write('stderr ✓')",
], options)
assert.equal(String(output.stdout), 'stdout ✓')
assert.equal(String(output.stderr), 'stderr ✓')

// A real failing process tests callback code/output transport, including UTF-8.
await assert.rejects(execFileWithDiagnostics(process.execPath, ['-e',
  "process.stdout.write('before failure ✓'); process.stderr.write('CIM failure ✓'); process.exitCode = 7",
], options), error => {
  assert.equal(error.cause.code, 7)
  assert.equal(error.subprocess.code, 7)
  assert.equal(error.subprocess.killed, false)
  assert.equal(error.subprocess.signal, null)
  assert.equal(error.subprocess.stdout, 'before failure ✓')
  assert.equal(error.subprocess.stderr, 'CIM failure ✓')
  assert.ok(error.subprocess.pid > 0)
  assert.equal(error.subprocess.timeoutMs, 20_000)
  assert.equal(error.subprocess.killSignal, 'SIGKILL')
  assert.ok(error.subprocess.elapsedMs >= 0)
  const serialized = error.message.split('\nDESKTOP_E2E_SUBPROCESS_FAILED: ')[1]
  assert.deepEqual(JSON.parse(serialized), error.subprocess,
    'phase diagnostics that keep only the message must retain the complete evidence')
  return true
})

// Spawn failure must remain distinguishable from a process exit or a timeout.
await assert.rejects(execFileWithDiagnostics(
  join(tmpdir(), `opensquilla-missing-${randomUUID()}.exe`), [], options,
), error => {
  assert.equal(error.cause.code, 'ENOENT')
  assert.equal(error.subprocess.code, 'ENOENT')
  assert.equal(error.subprocess.pid, null)
  assert.notEqual(error.subprocess.killed, true)
  return true
})

// Inject the native timeout callback rather than waiting 20 seconds in CI.
// Keep the actual configured limit, arguments and original error unchanged.
const timedOut = Object.assign(new Error('native cleanup command failed'), {
  code: null, killed: true, signal: 'SIGKILL',
})
const args = ['-NoProfile', '-Command', 'Get-CimInstance Win32_Process']
let calls = 0
await assert.rejects(execFileWithDiagnostics('powershell.exe', args, options, {
  execFileImpl(executable, actualArgs, actualOptions, callback) {
    calls += 1
    assert.equal(executable, 'powershell.exe')
    assert.equal(actualArgs, args)
    assert.equal(actualOptions, options)
    queueMicrotask(() => callback(timedOut, Buffer.from('partial output'), Buffer.from('native error')))
    return { pid: 1234 }
  },
}), error => {
  assert.equal(error.cause, timedOut)
  assert.equal(error.subprocess.code, null)
  assert.equal(error.subprocess.killed, true)
  assert.equal(error.subprocess.signal, 'SIGKILL')
  assert.equal(error.subprocess.pid, 1234)
  assert.equal(error.subprocess.stdout, 'partial output')
  assert.equal(error.subprocess.stderr, 'native error')
  return true
})
assert.equal(calls, 1, 'a failed cleanup command must never be retried or accepted')

console.log('Desktop E2E subprocess diagnostics checks passed')
