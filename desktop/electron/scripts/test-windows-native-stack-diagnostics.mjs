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

async function within(promise, timeoutMs, message) {
  let timer
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), timeoutMs)
    })])
  } finally { clearTimeout(timer) }
}

function literal(value) {
  return `'${value.replace(/'/g, "''")}'`
}

function writeRecord(record) {
  return `[Console]::Out.WriteLine(${literal(JSON.stringify(record))})\n`
}

function decodePublicDebuggerHelp(bytes) {
  let encoding = 'utf8'
  let offset = 0
  if (bytes[0] === 0xff && bytes[1] === 0xfe) { encoding = 'utf16le-bom'; offset = 2 }
  else if (bytes[0] === 0xfe && bytes[1] === 0xff) { encoding = 'utf16be-bom'; offset = 2 }
  else if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) { encoding = 'utf8-bom'; offset = 3 }
  else if (bytes.length >= 32) {
    const pairs = Math.floor(bytes.length / 2)
    let evenNuls = 0, oddNuls = 0, evenAscii = 0, oddAscii = 0
    const isHelpAscii = byte => byte === 9 || byte === 10 || byte === 13 || (byte >= 32 && byte <= 126)
    for (let index = 0; index < pairs * 2; index += 2) {
      evenNuls += Number(bytes[index] === 0)
      oddNuls += Number(bytes[index + 1] === 0)
      evenAscii += Number(isHelpAscii(bytes[index]))
      oddAscii += Number(isHelpAscii(bytes[index + 1]))
    }
    if (oddNuls / pairs >= 0.8 && evenNuls / pairs <= 0.05 && evenAscii / pairs >= 0.7) encoding = 'utf16le-strong-nul-pattern'
    else if (evenNuls / pairs >= 0.8 && oddNuls / pairs <= 0.05 && oddAscii / pairs >= 0.7) encoding = 'utf16be-strong-nul-pattern'
  }
  const payload = bytes.subarray(offset)
  const text = encoding.startsWith('utf16be')
    ? Buffer.from(payload.subarray(0, payload.length - payload.length % 2)).swap16().toString('utf16le')
    : payload.toString(encoding.startsWith('utf16le') ? 'utf16le' : 'utf8')
  return { encoding, text }
}

const cdbSwitchCapabilityVariants = Object.freeze({
  noshell: Object.freeze(['-noshell', '-?']),
  nosqm: Object.freeze(['-nosqm', '-?']),
  'netsym-colon': Object.freeze(['-netsym:no', '-?']),
  'netsyms-colon': Object.freeze(['-netsyms:no', '-?']),
  'netsym-separated': Object.freeze(['-netsym', 'no', '-?']),
  'netsyms-separated': Object.freeze(['-netsyms', 'no', '-?']),
})

function summarizePublicSwitchCapability(text) {
  const errorLine = text.split(/\r?\n/).find(line =>
    /^(?:cdb(?:\.exe)?:\s*)?(?:invalid switch|unknown (?:switch|option)|unrecognized (?:switch|option)|error\b|unable to\b|command line error\b)/i.test(line.trim()))
  const helpStart = text.search(/^[ \t]*usage[ \t]*:/im)
  return {
    errorFirstLine: errorLine?.slice(0, 512) ?? null,
    errorFirstLineTruncated: Boolean(errorLine && errorLine.length > 512),
    helpPresent: helpStart !== -1,
    helpSha256: helpStart === -1 ? null : createHash('sha256').update(text.slice(helpStart)).digest('hex'),
    outputSha256: createHash('sha256').update(text).digest('hex'),
  }
}

/**
 * @param {string} directory
 * @param {{kind: 'capabilities'} | {kind: 'switch-capability', variant: keyof typeof cdbSwitchCapabilityVariants}
 *   | {kind: 'initialization', child: import('node:child_process').ChildProcess,
 *   identity: {electronPid: number, windowsStartTimeTicks: string}}} mode
 */
async function captureCdbCapabilities(directory, mode = { kind: 'capabilities' }) {
  assert.ok(['capabilities', 'switch-capability', 'initialization'].includes(mode?.kind), 'Only fixed CDB selftest modes are supported')
  const initialization = mode.kind === 'initialization'
  const switchCapability = mode.kind === 'switch-capability'
  if (switchCapability) assert.ok(Object.hasOwn(cdbSwitchCapabilityVariants, mode.variant), 'Only fixed public switch variants are supported')
  if (initialization) {
    assert.ok(Number.isSafeInteger(mode.identity?.electronPid) && mode.identity.electronPid > 0)
    assert.equal(mode.child?.pid, mode.identity.electronPid, 'Initialization can only inspect its owned Node child')
    assert.equal(mode.child.exitCode, null)
    assert.equal(mode.child.signalCode, null)
    assert.equal(mode.child.connected, true, 'Owned Node IPC must still be connected')
    assert.ok(typeof mode.identity.windowsStartTimeTicks === 'string' && /^\d{15,20}$/.test(mode.identity.windowsStartTimeTicks))
  }
  const control = initialization ? 'native-cdb-initialization'
    : switchCapability ? 'native-cdb-switch-capability' : 'native-cdb-capabilities'
  const maxBytes = (initialization || switchCapability ? 16 : 64) * 1024
  const sdkRoot = process.env['ProgramFiles(x86)']
  const debuggerPath = sdkRoot && join(sdkRoot, 'Windows Kits', '10', 'Debuggers', 'x64', 'cdb.exe')
  let toolBytes
  try {
    toolBytes = debuggerPath ? await readFile(debuggerPath) : null
  } catch (error) {
    if (error.code !== 'ENOENT') throw new Error('Cannot read the SDK CDB capability tool')
  }
  if (!toolBytes) {
    console.log(`SKIP ${control}: Windows SDK CDB is unavailable`)
    return { status: 'unavailable' }
  }
  const sha256 = createHash('sha256').update(toolBytes).digest('hex')
  const workDirectory = join(directory, initialization ? 'cdb-initialization-work'
    : switchCapability ? `cdb-switch-${mode.variant}-work` : 'cdb-capabilities-work')
  await mkdir(workDirectory)
  const symbolDirectory = join(workDirectory, 'symbols')
  if (initialization) await mkdir(symbolDirectory)
  const env = {}
  for (const name of ['SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA']) {
    if (process.env[name] !== undefined) env[name] = process.env[name]
  }
  // No arbitrary argv: fixed public usage variants or noninvasive initialization of
  // this test's credential-free Node child, with only a fixed marker and detach.
  // Never request stack frames, registers, memory, dumps, or product inspection.
  const args = initialization ? [
    '-pvr', '-noshell', '-nosqm', '-sins', '-ses', '-netsyms:no',
    '-y', symbolDirectory, '-p', String(mode.identity.electronPid),
    '-c', '.echo OPENSQUILLA_NATIVE_INIT_READY;qd',
  ] : switchCapability ? cdbSwitchCapabilityVariants[mode.variant] : ['-?']
  const child = spawn(debuggerPath, args, {
    cwd: workDirectory, env, windowsHide: true, stdio: [initialization ? 'pipe' : 'ignore', 'pipe', 'pipe'],
  })
  const stdout = [], stderr = []
  let stdoutBytes = 0, stderrBytes = 0, capturedBytes = 0, nulByteCount = 0
  let closeObserved = false, spawnFailed = false, outputLimited = false, timedOut = false
  const closed = new Promise(resolve => child.once('close', (...result) => { closeObserved = true; resolve(result) }))
  child.on('error', () => { spawnFailed = true })
  const killOwnedHelp = () => {
    if (!closeObserved && child.exitCode === null && child.signalCode === null) {
      try { child.kill('SIGKILL') } catch { /* final close observation records failure */ }
    }
  }
  const consume = (chunks, chunk, isStdout) => {
    if (isStdout) stdoutBytes += chunk.length
    else stderrBytes += chunk.length
    for (const byte of chunk) nulByteCount += Number(byte === 0)
    const remaining = Math.max(0, maxBytes - capturedBytes)
    if (remaining) {
      const kept = chunk.subarray(0, remaining)
      chunks.push(kept)
      capturedBytes += kept.length
    }
    if (stdoutBytes + stderrBytes > maxBytes) { outputLimited = true; killOwnedHelp() }
  }
  child.stdout.on('data', chunk => consume(stdout, chunk, true))
  child.stderr.on('data', chunk => consume(stderr, chunk, false))
  try {
    await within(closed, 3_000, 'SDK CDB selftest command timed out')
  } catch { timedOut = true }
  finally {
    if (!closeObserved) killOwnedHelp()
    await within(closed, 2_000, 'SDK CDB selftest cleanup timed out').catch(() => {})
    child.stdin?.destroy()
    child.stdout.destroy()
    child.stderr.destroy()
    child.unref()
  }
  const stdoutBuffer = Buffer.concat(stdout), stderrBuffer = Buffer.concat(stderr)
  const out = decodePublicDebuggerHelp(stdoutBuffer), err = decodePublicDebuggerHelp(stderrBuffer)
  const helpText = out.text + (out.text && err.text ? '\n' : '') + err.text
  const version = helpText.match(/\bVersion\s+(\d+(?:\.\d+){3})\b/i)?.[1] ?? null
  const initializationReady = helpText.split(/\r?\n/).some(line => line.trim() === 'OPENSQUILLA_NATIVE_INIT_READY')
  const capabilities = { control,
    status: !closeObserved ? 'containment-failed' : spawnFailed ? 'spawn-error'
      : outputLimited ? 'output-limit' : timedOut ? 'timeout'
        : initialization && (child.exitCode !== 0 || child.signalCode !== null) ? 'exit-error'
          : initialization && !initializationReady ? 'marker-missing' : 'complete',
    version, sha256, exitCode: child.exitCode, signal: child.signalCode, closeObserved,
    stdoutBytes, stderrBytes, capturedBytes, nulByteCount,
    stdoutFirst2Hex: stdoutBuffer.subarray(0, 2).toString('hex'), stderrFirst2Hex: stderrBuffer.subarray(0, 2).toString('hex'),
    stdoutEncoding: out.encoding, stderrEncoding: err.encoding,
    ...(initialization ? {
      ownedIdentity: { electronPid: mode.identity.electronPid, windowsStartTimeTicks: mode.identity.windowsStartTimeTicks },
      initializationReady,
      stdoutText: out.text, stderrText: err.text,
    } : switchCapability ? { variant: mode.variant, ...summarizePublicSwitchCapability(helpText) } : { helpText }) }
  console.log(JSON.stringify(capabilities))
  assert.equal(closeObserved, true, 'Owned SDK selftest child must close within its cleanup deadline')
  return capabilities
}

const syntheticUsage = 'Microsoft Windows Debugger Version 10.0.26100.1\r\nUsage: cdb -?\r\n'
assert.deepEqual(decodePublicDebuggerHelp(Buffer.from(syntheticUsage)), { encoding: 'utf8', text: syntheticUsage })
assert.deepEqual(decodePublicDebuggerHelp(Buffer.from(syntheticUsage, 'utf16le')),
  { encoding: 'utf16le-strong-nul-pattern', text: syntheticUsage })
assert.deepEqual(decodePublicDebuggerHelp(Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(syntheticUsage, 'utf16le')])),
  { encoding: 'utf16le-bom', text: syntheticUsage })
assert.deepEqual(decodePublicDebuggerHelp(Buffer.concat([Buffer.from([0xfe, 0xff]), Buffer.from(syntheticUsage, 'utf16le').swap16()])),
  { encoding: 'utf16be-bom', text: syntheticUsage })

if (process.platform !== 'win32') {
  assert.deepEqual(await captureWindowsNativeStacks({}), { status: 'unsupported-platform' })
  console.log('SKIP Windows native stack controls: unsupported platform')
} else {
  const directory = await mkdtemp(join(tmpdir(), 'opensquilla-native-stacks-test-'))
  const helperPath = join(directory, 'helper.ps1')
  const descendantPath = join(directory, 'descendant.json')
  const targetEnv = {}
  for (const name of ['SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA']) {
    if (process.env[name] !== undefined) targetEnv[name] = process.env[name]
  }
  const target = spawn(process.execPath, ['-e', `
    process.on('message', message => {
      if (message === 'quit') process.exit(0);
      if (message === 'ping') process.send('pong');
    });
    setInterval(() => {}, 1000);
    process.send('ready');
  `], { env: targetEnv, windowsHide: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] })
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
    // Capability discovery runs first, with only -? and no target parameters.
    const capabilities = await captureCdbCapabilities(directory)
    // Six fixed, target-free public-help commands identify the SDK grammar.
    // A captured help response or its exit code never establishes flag support.
    for (const variant of Object.keys(cdbSwitchCapabilityVariants)) {
      const result = await captureCdbCapabilities(directory, { kind: 'switch-capability', variant })
      if (result.sha256 && capabilities.sha256) assert.equal(result.sha256, capabilities.sha256)
    }
    const initialization = await captureCdbCapabilities(directory, { kind: 'initialization', child: target, identity })
    // Initialization failure is diagnostic evidence, never a substitute for
    // the unchanged real native-capture assertions that follow.
    await targetResponsive()
    if (initialization.sha256 && capabilities.sha256) assert.equal(initialization.sha256, capabilities.sha256)
    const native = await captureWindowsNativeStacks(identity)
    // These records have already passed the collector's metadata whitelist.
    // Preserve tool/version/phase/exit evidence even when the control fails.
    console.log(JSON.stringify({ control: 'native-cdb-capture', native }))
    const nativeTool = native.records.find(record => record.kind === 'tool')
    if (nativeTool && capabilities.sha256) {
      assert.equal(nativeTool.sha256, capabilities.sha256, 'Capability help and capture must use the same SDK CDB bytes')
      if (capabilities.version) assert.equal(nativeTool.version, capabilities.version)
    }
    if (nativeTool && initialization.sha256) assert.equal(nativeTool.sha256, initialization.sha256)
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
        '-pvr', '-noshell', '-nosqm', '-sins', '-ses', '-netsyms:no',
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
      const debuggerClosed = new Promise(resolve => debuggerChild.once('close', (...result) => resolve(result)))
      try {
        const ackDeadline = Date.now() + 5_000
        while (!acknowledged && Date.now() < ackDeadline && !spawnFailed
          && debuggerChild.exitCode === null && rawBytes <= 256 * 1024) await delay(10)
        assert.equal(acknowledged, true, 'Owned CDB must acknowledge attachment before forced-close control')
        assert.equal(spawnFailed, false)
        assert.ok(rawBytes <= 256 * 1024, 'Owned CDB raw output must remain bounded and private')
        await targetResponsive()
        assert.equal(debuggerChild.exitCode, null)
        assert.equal(debuggerChild.kill('SIGKILL'), true)
        const [exitCode, signal] = await within(debuggerClosed, 3_000, 'Owned CDB force control did not close')
        assert.ok((Number.isInteger(exitCode) && exitCode !== 0) || signal === 'SIGKILL')
        await assertExited(debuggerChild.pid)
        await targetResponsive()
        console.log(JSON.stringify({ control: 'native-cdb-forced-close', exitCode, signal, targetResponsive: true }))
        checks++
      } finally {
        try {
          if (debuggerChild.exitCode === null && debuggerChild.signalCode === null) debuggerChild.kill('SIGKILL')
          await within(debuggerClosed, 3_000, 'Owned CDB fixture cleanup did not close')
        } finally {
          debuggerChild.stdin.destroy()
          debuggerChild.stdout.destroy()
          debuggerChild.stderr.destroy()
        }
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
