// Native preflight. This does not launch Electron, Gateway, or an installer.
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs/promises'
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const pythonScript = fileURLToPath(new URL('./native-audit-write-view.py', import.meta.url))
const samePath = (a, b) => process.platform === 'win32'
  ? resolve(a).toLowerCase() === resolve(b).toLowerCase() : resolve(a) === resolve(b)

function python(pythonExecutable, request) {
  const child = spawnSync(pythonExecutable, [pythonScript], {
    input: JSON.stringify(request), encoding: 'utf8', shell: false, windowsHide: true,
    timeout: 15_000, maxBuffer: 64 * 1024,
  })
  if (child.error || child.signal || child.status !== 0) {
    let result
    try { result = JSON.parse(child.stdout) } catch { result = { error: child.stderr || child.stdout } }
    const error = new Error(`Python probe failed: ${child.error?.message || result.error || child.status}`)
    error.probeResult = result
    throw error
  }
  const result = JSON.parse(child.stdout)
  assert.equal(result.ok, true)
  assert.ok(samePath(result.executable, pythonExecutable), 'Python executable changed')
  return result
}

async function absent(path) {
  try { await fs.lstat(path) } catch (error) { if (error.code === 'ENOENT') return; throw error }
  throw new Error(`Refusing to reuse a probe directory: ${path}`)
}

function allowedDestination(roaming, requested, actual) {
  if (samePath(actual, requested)) return
  const parts = relative(join(dirname(roaming), 'Local', 'Packages'), actual).split(sep)
  assert.ok(parts.length === 4 && parts[0] !== '..' && parts[0] !== '' &&
    parts[1] === 'LocalCache' && parts[2] === 'Roaming' && parts[3] === basename(requested),
  `Unexpected created destination; preserve it: ${actual}`)
}

export async function runNativeWriteView({ roamingParent, pythonExecutable }) {
  assert.ok(isAbsolute(roamingParent) && isAbsolute(pythonExecutable), 'Absolute inputs required')
  roamingParent = resolve(roamingParent)
  const parent = await fs.lstat(roamingParent)
  assert.ok(parent.isDirectory() && !parent.isSymbolicLink(), 'Roaming parent must be ordinary')
  assert.ok(samePath(await fs.realpath(roamingParent), roamingParent), 'Roaming parent redirected')
  const auditId = randomUUID()
  const report = {
    schemaVersion: 1, auditId, nodeExecutable: process.execPath, pythonExecutable,
    roamingParent, writesPerformed: false, nativeWriteViewVerified: false,
    observations: [], cleanup: [], retainedPaths: [], error: null,
    boundary: 'Only these fresh siblings in this launch context are verified; later redirection remains possible. The final cache canonical guard is required.',
  }
  const request = role => ({ auditId, role, roamingParent })
  const owned = []
  const nodeDirectory = join(roamingParent, `opensquilla-native-write-node-${auditId}`)
  const pythonDirectory = join(roamingParent, `opensquilla-native-write-python-${auditId}`)
  try {
    await absent(nodeDirectory)
    await absent(pythonDirectory)
    await fs.mkdir(nodeDirectory)
    report.writesPerformed = true
    // Record immediately, including an incomplete create, so failure never
    // silently loses an owned path. Unknown destinations are never cleaned.
    const nodeOwned = { ...request('node'), requested: nodeDirectory, actual: null, receipt: null }
    owned.push(nodeOwned)
    nodeOwned.actual = await fs.realpath(nodeDirectory)
    allowedDestination(roamingParent, nodeDirectory, nodeOwned.actual)
    // Python validates the actual destination before any cleanup. The bounded
    // marker content contains only this random UUID and fixed writer role.
    await fs.writeFile(join(nodeDirectory, 'marker.txt'),
      `OpenSquilla native write-view v1 ${auditId} node\n`, { flag: 'wx' })
    const nodeReceipt = python(pythonExecutable, { ...request('node'), action: 'inspect' }).receipt
    nodeOwned.receipt = nodeReceipt
    report.observations.push(nodeReceipt)
    assert.ok(samePath(nodeReceipt.actual, nodeOwned.actual), 'Node/Python directory views disagree')
    assert.equal(nodeReceipt.native, true, 'Node write is redirected; restart from an ordinary desktop shell')
    const pythonOwned = { ...request('python'), requested: pythonDirectory, actual: null, receipt: null }
    owned.push(pythonOwned)
    let pythonReceipt
    try {
      pythonReceipt = python(pythonExecutable, { ...request('python'), action: 'create' }).receipt
    } catch (error) {
      pythonOwned.actual = error.probeResult?.createdPath?.actual ?? null
      throw error
    }
    pythonOwned.actual = pythonReceipt.actual
    pythonOwned.receipt = pythonReceipt
    report.observations.push(pythonReceipt)
    assert.equal(pythonReceipt.native, true, 'Python write is redirected; restart from an ordinary desktop shell')
    const nodePythonDirectory = await fs.realpath(pythonDirectory)
    const nodePythonMarker = await fs.realpath(join(pythonDirectory, 'marker.txt'))
    assert.ok(samePath(nodePythonDirectory, pythonDirectory), 'Node sees a redirected Python directory')
    assert.ok(samePath(nodePythonMarker, join(pythonDirectory, 'marker.txt')), 'Node sees a redirected Python marker')
    assert.equal(await fs.readFile(nodePythonMarker, 'utf8'),
      `OpenSquilla native write-view v1 ${auditId} python\n`, 'Python/Node marker mismatch')
    report.nativeWriteViewVerified = true
  } catch (error) {
    report.error = `${error.name}: ${error.message}`
  } finally {
    for (const entry of owned.reverse()) {
      try {
        assert.ok(entry.receipt, 'Missing creation receipt; cleanup cannot be proved safe')
        const result = python(pythonExecutable, { ...request(entry.role), action: 'cleanup', receipt: entry.receipt })
        report.cleanup.push({ path: result.removed, removed: true })
      } catch (error) {
        const path = entry.actual ?? entry.requested
        report.cleanup.push({ path, removed: false, error: `${error.name}: ${error.message}` })
        report.retainedPaths.push(path)
        report.nativeWriteViewVerified = false
        report.error ??= 'Probe cleanup could not be proved safe; paths retained for operator review'
      }
    }
  }
  return report
}

if (process.argv[1] && samePath(process.argv[1], fileURLToPath(import.meta.url))) {
  try {
    assert.equal(process.argv.length, 4, 'Expected the Roaming parent and frozen Python absolute path')
    const report = await runNativeWriteView({ roamingParent: process.argv[2], pythonExecutable: process.argv[3] })
    process.stdout.write(`${JSON.stringify(report)}\n`)
    process.exitCode = report.nativeWriteViewVerified ? 0 : 1
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ nativeWriteViewVerified: false, error: `${error.name}: ${error.message}` })}\n`)
    process.exitCode = 1
  }
}
