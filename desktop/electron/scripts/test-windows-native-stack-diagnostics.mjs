import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { captureWindowsNativeStacks } from './windows-native-stack-diagnostics.mjs'
import { captureWindowsProcessStart } from './windows-wait-chain-diagnostics.mjs'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'

function exists(pid) {
  try { process.kill(pid, 0); return true } catch (error) {
    if (error.code === 'ESRCH') return false
    throw error
  }
}

async function assertExited(pid) {
  const deadline = Date.now() + 5_000
  while (exists(pid) && Date.now() < deadline) await delay(25)
  assert.equal(exists(pid), false, 'Owned diagnostic fixture must be reaped')
}

function literal(value) {
  return `'${value.replace(/'/g, "''")}'`
}

function writeRecord(record) {
  return `[Console]::Out.WriteLine(${literal(JSON.stringify(record))})\n`
}

if (process.platform !== 'win32') {
  assert.deepEqual(await captureWindowsNativeStacks({}), { status: 'unsupported-platform' })
  console.log('SKIP Windows native stack controls: unsupported platform')
} else {
  const directory = await mkdtemp(join(tmpdir(), 'opensquilla-native-stacks-test-'))
  const helperPath = join(directory, 'helper.ps1')
  const descendantPath = join(directory, 'descendant.json')
  const target = spawn(process.execPath, ['-e', `
    process.on('message', message => {
      if (message === 'quit') process.exit(0);
      if (message === 'ping') process.send('pong');
    });
    setInterval(() => {}, 1000);
    process.send('ready');
  `], { windowsHide: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] })
  let descendantIdentity
  let checks = 0
  async function targetResponsive() {
    assert.equal(target.exitCode, null, 'Diagnostics must never terminate their target')
    const reply = once(target, 'message', { signal: AbortSignal.timeout(3_000) })
    target.send('ping')
    assert.equal((await reply)[0], 'pong')
  }
  async function captureFixture(source, options = {}) {
    await writeFile(helperPath, `param($TargetPid,$ExpectedStartTicks)\n${source}`)
    return captureWindowsNativeStacks(identity, { helperPath, ...options })
  }
  let identity
  try {
    assert.equal((await once(target, 'message', { signal: AbortSignal.timeout(5_000) }))[0], 'ready')
    const start = await captureWindowsProcessStart(target.pid)
    assert.equal(start.status, 'complete')
    identity = { electronPid: target.pid, windowsStartTimeTicks: start.startTicks }

    for (const invalid of [undefined, {}, { ...identity, electronPid: String(target.pid) },
      { ...identity, electronPid: -1 }, { ...identity, electronPid: 0x1_0000_0000 },
      { ...identity, windowsStartTimeTicks: Number(start.startTicks) },
      { ...identity, windowsStartTimeTicks: `${start.startTicks}\n` }]) {
      assert.deepEqual(await captureWindowsNativeStacks(invalid), { status: 'invalid-identity' })
    }
    for (const timeoutMs of [0, -1, NaN, Infinity, 5_001, 1.5]) {
      assert.deepEqual(await captureWindowsNativeStacks(identity, { timeoutMs }), { status: 'invalid-options' })
    }
    checks++

    const phase = { kind: 'phase', phase: 'identity-verified', pid: target.pid, elapsedMs: 0 }
    const tool = { kind: 'tool', name: 'cdb', version: '10.0.26100.1', sha256: '0'.repeat(64) }
    const frame = { kind: 'frame', pid: target.pid, tid: 123, index: 0, module: 'node', symbol: 'uv_run', offset: '0x1' }
    const completion = { kind: 'completion', pid: target.pid, status: 'complete',
      threadCount: 1, threadsTruncated: false, frameCount: 1, stdoutBytes: 80, stderrBytes: 0,
      discardedLines: 0, elapsedMs: 1, cdbPid: 124, cdbExitCode: 0, cdbExitObserved: true }
    const prefix = [phase, tool, frame].map(record => writeRecord({ ...record, raw: 'RAW_CANARY_DO_NOT_RETURN' })).join('')
    const filtered = await captureFixture(`${prefix}${writeRecord({ ...completion, raw: 'RAW_CANARY_DO_NOT_RETURN' })}
[Console]::Error.WriteLine('RAW_CANARY_STDERR_DO_NOT_RETURN')
`)
    assert.equal(filtered.status, 'complete')
    assert.deepEqual(filtered.records, [phase, tool, frame, completion])
    assert.equal(JSON.stringify(filtered).includes('RAW_CANARY'), false)
    assert.equal(Object.hasOwn(filtered, 'stdout'), false)
    assert.equal(Object.hasOwn(filtered, 'stderr'), false)
    checks++

    const unavailableCompletion = { ...completion, status: 'unavailable', threadCount: 0, frameCount: 0,
      cdbPid: null, cdbExitCode: null, cdbExitObserved: false }
    const unavailable = await captureFixture(writeRecord(phase) + writeRecord(unavailableCompletion))
    assert.equal(unavailable.status, 'unavailable', 'Missing CDB must remain explicitly unavailable')
    checks++

    const invalid = await captureFixture(`${prefix}
${writeRecord({ ...frame, index: 1, symbol: 'RAW_CANARY(arg secret)' })}
${writeRecord({ ...frame, pid: target.pid + 1, index: 1 })}
${writeRecord({ ...completion, threadCount: 2 })}`)
    assert.equal(invalid.status, 'invalid-output')
    assert.equal(invalid.outputParse.invalidLines, 2)
    assert.equal(JSON.stringify(invalid).includes('RAW_CANARY'), false)
    checks++

    const partial = await captureFixture(`${prefix}
[Console]::Error.WriteLine('RAW_CANARY_STDERR_DO_NOT_RETURN')
[Console]::Out.Write(${literal(JSON.stringify(completion))})
[Console]::Out.Flush()
Start-Sleep -Seconds 60
`, { timeoutMs: 2_000 })
    assert.equal(partial.status, 'timeout')
    assert.equal(partial.outputParse.status, 'incomplete-tail')
    assert.deepEqual(partial.records, [phase, tool, frame], 'Non-newline completion cannot promote partial capture')
    assert.equal(JSON.stringify(partial).includes('RAW_CANARY'), false)
    assert.equal(partial.containment, 'complete')
    await assertExited(partial.helperPid)
    await targetResponsive()
    checks++

    const started = Date.now()
    const stalled = await captureFixture(`
$descendant = Start-Process -FilePath ${literal(process.execPath)} -ArgumentList @('-e', '"setInterval(()=>{},1000)"') -PassThru -WindowStyle Hidden
@{pid=$descendant.Id;startTicks=$descendant.StartTime.ToUniversalTime().Ticks.ToString()} | ConvertTo-Json -Compress | Set-Content -LiteralPath ${literal(descendantPath)}
[Console]::Out.WriteLine((@{kind='phase';phase='cdb-started';pid=[int]$TargetPid;elapsedMs=0;cdbPid=$descendant.Id} | ConvertTo-Json -Compress))
[Console]::Out.Flush()
Start-Sleep -Seconds 60
`, { timeoutMs: 2_000 })
    descendantIdentity = JSON.parse(await readFile(descendantPath, 'utf8'))
    assert.equal(stalled.status, 'timeout')
    assert.equal(stalled.helperTreeReaped, true)
    assert.equal(stalled.helperExitObserved, true)
    assert.equal(stalled.helperCloseObserved, true)
    assert.equal(stalled.containment, 'complete')
    assert.ok(Date.now() - started < 4_750, 'Two-second fixture timeout plus two-second containment must stay bounded')
    assert.ok(stalled.records.some(record => record.kind === 'phase' && record.cdbPid === descendantIdentity.pid))
    await assertExited(stalled.helperPid)
    await assertExited(descendantIdentity.pid)
    await targetResponsive()
    checks++

    const oversized = await captureFixture(`${prefix}
[Console]::Out.Write('RAW_CANARY_OUTPUT_LIMIT' * 14000)
[Console]::Out.Flush()
Start-Sleep -Seconds 60
`)
    assert.equal(oversized.status, 'output-limit')
    assert.deepEqual(oversized.records, [phase, tool, frame])
    assert.equal(oversized.containment, 'complete', JSON.stringify(oversized))
    assert.equal(JSON.stringify(oversized).includes('RAW_CANARY'), false)
    await assertExited(oversized.helperPid)
    await targetResponsive()
    checks++

    const recordLimit = await captureFixture(writeRecord(phase).repeat(2_201) + writeRecord(unavailableCompletion))
    assert.equal(recordLimit.status, 'invalid-output')
    assert.equal(recordLimit.outputParse.status, 'record-limit')
    assert.equal(recordLimit.records.length, 2_200)
    checks++

    const duplicate = await captureFixture(prefix + writeRecord(frame) + writeRecord(completion))
    assert.equal(duplicate.status, 'invalid-output')
    assert.equal(duplicate.outputParse.invalidLines, 1)
    checks++

    // These use the actual helper, not a canned result. Identity rejection must
    // precede tool discovery and leave this owned target responsive.
    const mismatch = await captureWindowsNativeStacks({ ...identity,
      windowsStartTimeTicks: String(BigInt(identity.windowsStartTimeTicks) + 1n) })
    assert.equal(mismatch.status, 'identity-mismatch')
    assert.ok(mismatch.records.some(record => record.kind === 'target' && record.status === 'identity-mismatch'))
    assert.equal(mismatch.records.some(record => record.kind === 'frame'), false)
    const missing = await captureWindowsNativeStacks({ ...identity, electronPid: 2147483647 })
    assert.equal(missing.status, 'not-found')
    await targetResponsive()
    checks++

    // When the SDK exists, exercising CDB on this IPC-ready owned Node process
    // is mandatory. Only genuine missing-tool status skips the native positive.
    const native = await captureWindowsNativeStacks(identity)
    if (native.status === 'unavailable') {
      assert.equal(native.records.some(record => record.kind === 'frame'), false)
      console.log('SKIP native CDB positive: Windows SDK CDB is unavailable')
      console.log('SKIP native CDB forced-close control: Windows SDK CDB is unavailable')
    } else {
      assert.equal(native.status, 'complete', 'Available CDB must complete a real capture on an owned Node target')
      assert.ok(native.records.some(record => record.kind === 'frame'))
      assert.ok(native.records.some(record => record.kind === 'tool'))
      assert.equal(native.records.at(-1).cdbExitObserved, true)
      assert.equal(native.records.at(-1).cdbExitCode, 0)
      assert.equal(native.records.at(-1).frameCount, native.records.filter(record => record.kind === 'frame').length)
      assert.equal(native.helperExitObserved, true)
      assert.equal(native.helperCloseObserved, true)
      console.log('PASS native CDB capture: owned target, nonempty frames, collector exit observed')
      checks++

      // Real debugger force control uses the same existing SDK executable and
      // noninvasive flags, but a fixed ready marker without qd. Its stdin stays
      // open until this test kills only its own CDB ChildProcess handle.
      const debuggerPath = join(process.env['ProgramFiles(x86)'], 'Windows Kits', '10', 'Debuggers', 'x64', 'cdb.exe')
      assert.equal(createHash('sha256').update(await readFile(debuggerPath)).digest('hex'),
        native.records.find(record => record.kind === 'tool').sha256)
      const workDirectory = join(directory, 'cdb-owned-work')
      const symbolDirectory = join(workDirectory, 'symbols')
      await mkdir(symbolDirectory, { recursive: true })
      const debuggerEnv = {}
      for (const name of ['SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA']) {
        if (process.env[name] !== undefined) debuggerEnv[name] = process.env[name]
      }
      const debuggerChild = spawn(debuggerPath, [
        '-pvr', '-pd', '-noshell', '-nosqm', '-sins', '-ses', '-netsyms:no',
        '-y', symbolDirectory, '-p', String(target.pid), '-c', '.echo OPENSQUILLA_NATIVE_TEST_READY',
      ], { cwd: workDirectory, env: debuggerEnv, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
      let rawBytes = 0
      let pendingLine = ''
      let acknowledged = false
      let spawnFailed = false
      debuggerChild.on('error', () => { spawnFailed = true })
      debuggerChild.stdout.on('data', chunk => {
        rawBytes += chunk.length
        if (rawBytes > 256 * 1024) return
        const lines = (pendingLine + chunk.toString()).split(/\r?\n/)
        pendingLine = lines.pop().slice(-4_096)
        if (lines.some(line => line.trim() === 'OPENSQUILLA_NATIVE_TEST_READY')) acknowledged = true
      })
      debuggerChild.stderr.on('data', chunk => { rawBytes += chunk.length })
      const debuggerClosed = once(debuggerChild, 'close')
      try {
        const ackDeadline = Date.now() + 5_000
        while (!acknowledged && Date.now() < ackDeadline && !spawnFailed
          && debuggerChild.exitCode === null && rawBytes <= 256 * 1024) await delay(10)
        assert.equal(acknowledged, true, 'Owned CDB must acknowledge attachment before forced-close control')
        assert.equal(spawnFailed, false)
        assert.ok(rawBytes <= 256 * 1024, 'Owned CDB raw output must remain bounded and private')
        await targetResponsive()
        assert.equal(debuggerChild.exitCode, null)
        debuggerChild.kill('SIGKILL')
        const [exitCode, signal] = await Promise.race([
          debuggerClosed,
          delay(3_000).then(() => { throw new Error('Owned CDB force control did not close') }),
        ])
        assert.notEqual(exitCode, null)
        await assertExited(debuggerChild.pid)
        await targetResponsive()
        console.log(JSON.stringify({ control: 'native-cdb-forced-close', exitCode, signal, targetResponsive: true }))
        checks++
      } finally {
        if (debuggerChild.exitCode === null) debuggerChild.kill('SIGKILL')
        debuggerChild.stdin.destroy()
        debuggerChild.stdout.destroy()
        debuggerChild.stderr.destroy()
      }
    }
    await targetResponsive()
    const after = await captureWindowsProcessStart(target.pid)
    assert.equal(after.status, 'complete')
    assert.equal(after.startTicks, identity.windowsStartTimeTicks)
    const exited = once(target, 'close', { signal: AbortSignal.timeout(5_000) })
    target.send('quit')
    assert.deepEqual(await exited, [0, null])
    await assertExited(target.pid)
    console.log(`PASS Windows native stack controls: ${checks}; target exited naturally after validation`)
  } finally {
    if (target.exitCode === null) {
      await terminateWindowsProcessTree({ pid: target.pid, timeoutMs: 1_000, fallback: () => target.kill('SIGKILL') })
    }
    // Emergency fixture cleanup is separate from the collector. Its saved start
    // identity must still match before a test-created descendant may be killed.
    if (descendantIdentity && exists(descendantIdentity.pid)) {
      const current = await captureWindowsProcessStart(descendantIdentity.pid)
      if (current.status === 'complete' && current.startTicks === descendantIdentity.startTicks) {
        await terminateWindowsProcessTree({ pid: descendantIdentity.pid, timeoutMs: 1_000, fallback: () => {} })
      }
    }
    // Recursive removal is limited to this exact mkdtemp-owned test directory.
    assert.equal(dirname(directory), tmpdir())
    assert.ok(directory.startsWith(join(tmpdir(), 'opensquilla-native-stacks-test-')))
    await rm(directory, { recursive: true, force: true })
  }
}
