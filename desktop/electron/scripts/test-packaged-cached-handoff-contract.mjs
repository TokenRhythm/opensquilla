import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, dirname, join, relative, resolve, sep } from 'node:path'
import { test } from 'node:test'
import { spawn } from 'node:child_process'
import { EventEmitter, once } from 'node:events'
import { setTimeout as delay } from 'node:timers/promises'
import { CACHE_AUDIT_MARKER, CACHE_AUDIT_PURPOSE, assertCachedRestartEvidence, bytesSha256, copyCachedHandoffInstaller, requestCachedQuitOnce, validateCachedCandidate, verifyCachedHandoffInputs, waitForRestoredCache } from './fixtures/packaged-cached-handoff/contract.mjs'
import { observeSignedHandoff, preserveFailedDriverUntilExit, processExitObservation, releaseExitedHandoffTransport, signedHandoffLogEvidence, signedProcessSnapshot, trackSignedChildClose } from './fixtures/packaged-cached-handoff/signed-exit-observer.mjs'

// Inert files only: these tests never invoke a signature seam, signed native
// staging, Electron, a Gateway, or an installer. Native acceptance is separate.
const manifest = () => ({
  schemaVersion: 1, tag: 'v0.5.9002', version: '0.5.9002', baseVersion: '0.5.9002',
  prerelease: false, publishedAt: '2026-09-09T00:00:00Z',
  releaseUrl: 'https://github.com/TokenRhythm/opensquilla/releases/tag/v0.5.9002', sha256sums: 'SHA256SUMS',
  platforms: {
    'win32-x64': { feed: 'latest.yml', installer: 'OpenSquilla-0.5.9002-win-x64.exe' },
    'darwin-arm64': { feed: 'latest-mac.yml', archive: 'OpenSquilla-0.5.9002-mac-arm64.zip', installer: 'OpenSquilla-0.5.9002-mac-arm64.dmg' },
  },
})
async function fixture(t, temp = tmpdir()) {
  const canonicalTemp = await realpath(temp)
  const root = await mkdtemp(join(canonicalTemp, 'opensquilla-cached-handoff-contract-'))
  t.after(async () => {
    const suffix = relative(resolve(canonicalTemp), resolve(root))
    assert.ok(suffix.startsWith('opensquilla-cached-handoff-contract-') && !suffix.includes(sep))
    await rm(root, { recursive: true, force: true })
  })
  const userDataDir = join(root, 'synthetic-profile')
  const profile = join(userDataDir, 'opensquilla')
  await mkdir(profile, { recursive: true })
  const installer = Buffer.from('INERT CONTRACT DATA: NEVER EXECUTE')
  const config = Buffer.from('# synthetic config\n')
  const input = {
    userDataDir, baselineVersion: '0.5.9001', expectedVersion: '0.5.9002',
    sourceSha: 'b'.repeat(40), baselineSourceSha: 'a'.repeat(40), expectedSha256: bytesSha256(installer),
    manifestPath: join(root, 'channel.json'), installerPath: join(root, manifest().platforms['win32-x64'].installer),
  }
  const marker = { ...input, schemaVersion: 1, purpose: CACHE_AUDIT_PURPOSE, auditId: 'c'.repeat(32), seedLabel: 'signed-update-audit', configSha256: bytesSha256(config) }
  await writeFile(input.installerPath, installer)
  await writeFile(input.manifestPath, JSON.stringify(manifest()))
  await writeFile(join(profile, 'config.toml'), config)
  await writeFile(join(userDataDir, CACHE_AUDIT_MARKER), JSON.stringify(marker))
  return { root, input, marker, installer }
}

test('production channel parser and canonical metadata bind a forward fixture candidate', async t => {
  const f = await fixture(t)
  const plan = await verifyCachedHandoffInputs(f.input)
  assert.deepEqual(plan.descriptor, { schemaVersion: 1, tag: 'v0.5.9002', version: '0.5.9002', installer: basename(f.input.installerPath), sha256: f.input.expectedSha256, bytes: f.installer.length })
  assert.equal(plan.auditId, f.marker.auditId)
  assert.equal(plan.cacheDirectory, join(f.input.userDataDir, 'update-downloads'))
  assert.equal('path' in plan.descriptor, false)
})
test('channel canonical identity and actual forward version cannot be relaxed', async t => {
  const f = await fixture(t)
  for (const baselineVersion of ['0.5.3', '0.5.4', '0.5.9002', '0.5.9003', '0.5.9001-rc1']) {
    assert.throws(() => validateCachedCandidate({ ...f.input, baselineVersion }, manifest()))
  }
  for (const patch of [{ releaseUrl: 'https://attacker.invalid/b' }, { tag: 'v0.5.9003' }, { sha256sums: '../SHA256SUMS' }]) {
    assert.throws(() => validateCachedCandidate(f.input, { ...manifest(), ...patch }))
  }
  assert.throws(() => validateCachedCandidate({ ...f.input, installerPath: join(f.root, 'arbitrary.exe') }, manifest()))
})
test('source and candidate hashes must be fixed canonical values', async t => {
  const f = await fixture(t)
  for (const patch of [{ sourceSha: 'b'.repeat(39) }, { baselineSourceSha: '' }, { expectedSha256: 'B'.repeat(64) }, { expectedVersion: '00.5.9002' }]) {
    assert.throws(() => validateCachedCandidate({ ...f.input, ...patch }, manifest()))
  }
})
test('a stale or unrelated synthetic marker is rejected before staging', async t => {
  const f = await fixture(t)
  for (const patch of [{ expectedSha256: '0'.repeat(64) }, { sourceSha: 'd'.repeat(40) }, { purpose: 'unrelated' }, { userDataDir: f.root }, { configSha256: '0'.repeat(64) }]) {
    await writeFile(join(f.input.userDataDir, CACHE_AUDIT_MARKER), JSON.stringify({ ...f.marker, ...patch }))
    await assert.rejects(verifyCachedHandoffInputs(f.input))
  }
})
test('changed input bytes fail before a cache directory can be created', async t => {
  const f = await fixture(t)
  await writeFile(f.input.installerPath, 'changed')
  await assert.rejects(verifyCachedHandoffInputs(f.input), /approved Actions artifact/)
  await assert.rejects(readFile(join(f.input.userDataDir, 'update-downloads', 'windows-update-cache.json')), { code: 'ENOENT' })
})
test('exact copying is exclusive and never publishes an unsigned ready descriptor', async t => {
  const f = await fixture(t)
  const plan = await verifyCachedHandoffInputs(f.input)
  const copied = await copyCachedHandoffInstaller(plan)
  assert.deepEqual(await readFile(copied), f.installer)
  await assert.rejects(copyCachedHandoffInstaller(plan), { code: 'EEXIST' })
  await assert.rejects(readFile(join(plan.cacheDirectory, 'windows-update-cache.json')), { code: 'ENOENT' })
})
test('a source changed between validation and copying cannot publish ready metadata', async t => {
  const f = await fixture(t)
  const plan = await verifyCachedHandoffInputs(f.input)
  await writeFile(f.input.installerPath, Buffer.alloc(f.installer.length, 7))
  await assert.rejects(copyCachedHandoffInstaller(plan), /Staged bytes differ/)
  await assert.rejects(readFile(join(plan.cacheDirectory, 'windows-update-cache.json')), { code: 'ENOENT' })
})
test('redirected Temp is canonicalized for fixtures while aliases in audit inputs are rejected', async t => {
  const parent = await fixture(t)
  const target = join(parent.root, 'canonical-temp')
  const alias = join(parent.root, 'alias-temp')
  await mkdir(target)
  await symlink(target, alias, process.platform === 'win32' ? 'junction' : 'dir')
  const f = await fixture(t, alias)
  assert.equal(dirname(f.root), await realpath(target))
  await verifyCachedHandoffInputs(f.input)
  await assert.rejects(verifyCachedHandoffInputs({ ...f.input, userDataDir: join(alias, basename(f.root), 'synthetic-profile') }), /redirected paths/)
})
test('PowerShell BOM is parsed without changing marker byte provenance', async t => {
  const f = await fixture(t)
  const bytes = Buffer.from('\uFEFF' + JSON.stringify(f.marker))
  await writeFile(join(f.input.userDataDir, CACHE_AUDIT_MARKER), bytes)
  await writeFile(f.input.manifestPath, '\uFEFF' + JSON.stringify(manifest()))
  assert.equal((await verifyCachedHandoffInputs(f.input)).markerSha256, bytesSha256(bytes))
})
test('actual restart evidence requires a clean Quit, new process/Gateway identities and unchanged credentials', () => {
  const first = { electronPid: 41, gatewayStartIdentity: 'first', cacheRestored: true, normalQuitVerified: true }
  const second = { electronPid: 42, gatewayStartIdentity: 'second', cacheRestored: true }
  const hash = 'a'.repeat(64)
  assertCachedRestartEvidence(first, second, hash, hash)
  for (const patch of [{ electronPid: 41 }, { gatewayStartIdentity: 'first' }, { gatewayStartIdentity: null }, { cacheRestored: false }]) {
    assert.throws(() => assertCachedRestartEvidence(first, { ...second, ...patch }, hash, hash))
  }
  assert.throws(() => assertCachedRestartEvidence({ ...first, normalQuitVerified: false }, second, hash, hash))
  assert.throws(() => assertCachedRestartEvidence(first, second, hash, 'b'.repeat(64)))
})
test('bounded cache observation waits for pending verification and preserves the final real state', async () => {
  const states = [{ status: 'idle' }, { status: 'checking' }, { status: 'downloaded', latestVersion: '0.5.9002' }]
  let calls = 0
  assert.deepEqual(await waitForRestoredCache(async () => states[calls++], { timeoutMs: 1000, intervalMs: 1 }), states[2])
  assert.equal(calls, 3)
})
test('an actual signature error stops observation without retrying verification', async () => {
  let calls = 0
  await assert.rejects(waitForRestoredCache(async () => {
    calls += 1
    return { status: 'error', errorCode: 'signature_unavailable', error: 'actual Windows timeout' }
  }, { timeoutMs: 1000, intervalMs: 1 }), /actual Windows timeout/)
  assert.equal(calls, 1)
})
test('a stuck state request has a finite observation deadline', async () => {
  await assert.rejects(waitForRestoredCache(() => new Promise(() => {}), { timeoutMs: 20 }), /observation timed out/)
})
test('a failed Quit remains fenced against a cleanup retry', async () => {
  const state = { requested: false }
  let calls = 0
  const failedQuit = async () => { calls += 1; throw new Error('real Quit did not complete') }
  await assert.rejects(requestCachedQuitOnce(state, failedQuit), /did not complete/)
  assert.equal(await requestCachedQuitOnce(state, failedQuit), false)
  assert.equal(calls, 1)
})

const handoffRows = () => [
  { event: 'desktop_exit_phase', from: 'running', to: 'deferred', reason: 'verifying Windows update before installation' },
  { event: 'desktop_exit_phase', from: 'deferred', to: 'draining', reason: 'stopping Gateway for Windows installer' },
  { event: 'desktop_exit_phase', from: 'draining', to: 'committed', reason: 'Windows installer process started' },
  { event: 'update_windows_installer_handoff', version: '0.5.9002', tag: 'v0.5.9002', at: '2026-09-09T00:00:01Z' },
]
const logRows = rows => rows.map(row => JSON.stringify(row) + '\n').join('')
const observedProcesses = () => ['electron', 'wrapper', 'gateway', 'gateway-launcher'].map((role, index) => ({ pid: 100 + index, role, startIdentity: `windows-creation-filetime:${1000 + index}`, exited: true }))
const handoffObservation = overrides => ({
  processes: observedProcesses(), child: { exitCode: 0, signalCode: null }, checkpoint: '', expectedVersion: '0.5.9002',
  readLog: async () => logRows(handoffRows()), snapshot: async () => observedProcesses(), timeoutMs: 100, intervalMs: 1, ...overrides,
})

test('signed handoff observes exact process exits even when Playwright close never arrives', async () => {
  const evidence = await observeSignedHandoff(handoffObservation())
  assert.equal(evidence.electronExited, true)
  assert.equal(evidence.wrapperExited, true)
  assert.equal(evidence.allGatewayExited, true)
  assert.equal(evidence.handoffLogged, true)
  assert.equal('installerCompleted' in evidence, false)
})

test('a close event or one exited Gateway cannot substitute for all captured process exits', async () => {
  for (const role of ['electron', 'wrapper', 'gateway', 'gateway-launcher']) {
    await assert.rejects(observeSignedHandoff(handoffObservation({ timeoutMs: 15, snapshot: async () => observedProcesses().map(row => row.role === role ? { ...row, exited: false } : row) })), /timed out/)
  }
})

test('wrapper exit must be natural zero including a previously observed exit without a new event', async () => {
  for (const child of [{ exitCode: 1, signalCode: null }, { exitCode: 0, signalCode: 'SIGTERM' }]) {
    await assert.rejects(observeSignedHandoff(handoffObservation({ child })), /naturally/)
  }
  await assert.rejects(observeSignedHandoff(handoffObservation({ child: { exitCode: null, signalCode: null }, timeoutMs: 15 })), /timed out/)
  assert.equal((await observeSignedHandoff(handoffObservation({ child: { exitCode: 0, signalCode: null } }))).wrapperExited, true)
})

test('process disappearance and PID reuse use one comparable identity scheme and never kill', () => {
  const record = { pid: 101, role: 'electron', startIdentity: 'windows-creation-filetime:123' }
  const gone = () => { throw Object.assign(new Error('gone'), { code: 'ESRCH' }) }
  assert.equal(processExitObservation(record, gone, () => null).exited, true)
  assert.equal(processExitObservation(record, () => {}, () => 'windows-creation-filetime:124').recycled, true)
  for (const live of ['windows-creation-filetime:123', null, 'runtime-start:123', 'linux-proc-stat:124']) {
    assert.equal(processExitObservation(record, () => {}, () => live).exited, false)
  }
  for (const code of ['EPERM', 'EACCES', 'UNKNOWN']) {
    assert.equal(processExitObservation(record, () => { throw Object.assign(new Error(code), { code }) }, () => 'windows-creation-filetime:124').exited, false)
  }
  for (const startIdentity of [null, '', 'runtime-start:123']) {
    assert.throws(() => processExitObservation({ ...record, startIdentity }, gone, () => null), /pre-click/)
  }
  assert.throws(() => processExitObservation(record, () => {}, () => { throw new Error('query failed') }), /query failed/)
})

test('only this complete ordered install attempt can supply handoff log evidence', () => {
  const oldLog = logRows(handoffRows())
  assert.deepEqual(signedHandoffLogEvidence(oldLog, oldLog, '0.5.9002'), { committedExitLogged: false, handoffLogged: false, handoffAt: null })
  assert.throws(() => signedHandoffLogEvidence(oldLog, 'replaced\n', '0.5.9002'), /replaced or truncated/)
  assert.equal(signedHandoffLogEvidence('', oldLog.slice(0, -1), '0.5.9002').handoffLogged, false)
  const invalid = [
    handoffRows().slice(1),
    [handoffRows()[1], handoffRows()[0], ...handoffRows().slice(2)],
    handoffRows().map(row => row.to === 'committed' ? { ...row, reason: 'all lifecycle-owned Gateways exited' } : row),
    handoffRows().map(row => row.event === 'update_windows_installer_handoff' ? { ...row, version: '0.5.9003' } : row),
    handoffRows().map(row => row.event === 'update_windows_installer_handoff' ? { ...row, tag: 'v0.5.9003' } : row),
    [handoffRows()[0], { event: 'desktop_exit_phase', to: 'running', reason: 'update handoff did not commit' }, ...handoffRows()],
    [...handoffRows(), handoffRows()[3]],
    [...handoffRows(), { event: 'gateway_spawned', pid: 999 }],
  ]
  for (const rows of invalid) assert.throws(() => signedHandoffLogEvidence('', logRows(rows), '0.5.9002'))
  assert.throws(() => signedHandoffLogEvidence('', '{invalid}\n', '0.5.9002'))
})

test('snapshot, log read and unfinished click share one bounded handoff deadline', async () => {
  const pending = () => new Promise(() => {})
  for (const override of [{ snapshot: pending }, { readLog: pending }, { clickPromise: pending() }]) {
    await assert.rejects(observeSignedHandoff(handoffObservation({ ...override, timeoutMs: 15 })), /timed out/)
  }
})

test('handoff transport release refuses incomplete exit or log evidence before touching streams', async () => {
  let touched = false
  const child = { exitCode: 0, signalCode: null, stdio: [{ destroy() { touched = true } }], unref() { touched = true } }
  const evidence = { electronExited: true, wrapperExited: true, allGatewayExited: true, committedExitLogged: true, handoffLogged: true }
  for (const key of Object.keys(evidence)) {
    await assert.rejects(releaseExitedHandoffTransport(child, { ...evidence, [key]: false }), /independently verified/)
  }
  assert.equal(touched, false)
})

test('held transport is released only after independent proof and never becomes that proof', async () => {
  const child = new EventEmitter()
  let releases = 0
  let unrefs = 0
  Object.assign(child, { exitCode: 0, signalCode: null, unref() { unrefs += 1 }, stdio: [{ closed: false, destroy() { releases += 1; queueMicrotask(() => child.emit('close', 0, null)) } }] })
  trackSignedChildClose(child)
  const evidence = await observeSignedHandoff(handoffObservation({ child }))
  assert.equal(releases, 0)
  await releaseExitedHandoffTransport(child, evidence)
  assert.equal(releases, 1)
  assert.equal(unrefs, 1)
})

test('a stuck local transport release has its own finite cleanup failure', async () => {
  const child = new EventEmitter()
  Object.assign(child, { exitCode: 0, signalCode: null, unref() {}, stdio: [{ closed: false, destroy() {} }] })
  trackSignedChildClose(child)
  const evidence = await observeSignedHandoff(handoffObservation({ child }))
  await assert.rejects(releaseExitedHandoffTransport(child, evidence, { timeoutMs: 15 }), /transport did not release/)
  assert.equal(child.listenerCount('close'), 1, 'The original close tracker remains available to the failure fence')
  child.emit('close', 0, null)
  assert.equal(child.listenerCount('close'), 0)
})

test('inert child transport releases after natural exit without waiting for its grandchild', { timeout: 15_000 }, async () => {
  // Real OS pipe regression, with inert Node processes only. The grandchild
  // exits by its own short timer; no Electron, installer, profile or kill call.
  // Playwright opens two additional pipes at fd 3/4. A Node grandchild does
  // not reproduce NSIS' native handle behavior on Windows: close can already
  // have fired. The held-transport case above covers that independent branch.
  const code = `const {spawn}=require('node:child_process');const child=spawn(process.execPath,['-e',"process.send('ready');setTimeout(()=>{},1500)"],{stdio:['ignore','inherit','inherit',3,4,'ipc'],windowsHide:true});child.once('message',()=>{process.stdout.write('grandchild-ready\\n');child.disconnect();child.unref();});`
  const child = spawn(process.execPath, ['-e', code], { stdio: ['ignore', 'pipe', 'pipe', 'pipe', 'pipe'], windowsHide: true })
  trackSignedChildClose(child)
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', data => { stdout += data })
  child.stderr.on('data', data => { stderr += data })
  let closedBeforeRelease = false
  child.once('close', () => { closedBeforeRelease = true })
  const controller = new AbortController()
  try {
    await Promise.race([once(child, 'exit'), delay(10_000, undefined, { signal: controller.signal }).then(() => { throw new Error('Inert child did not exit naturally') })])
    assert.equal(child.exitCode, 0)
    assert.equal(stdout, 'grandchild-ready\n', `Grandchild must initialize before its parent exits: ${stderr}`)
    const closeAlreadyObserved = closedBeforeRelease
    const evidence = await observeSignedHandoff(handoffObservation({ child }))
    assert.equal(closedBeforeRelease, closeAlreadyObserved, 'Observation does not close transport')
    await releaseExitedHandoffTransport(child, evidence)
    assert.equal(closedBeforeRelease, true)
  } finally { controller.abort() }
})

test('closed streams cannot substitute for the actual child close event', async () => {
  const child = new EventEmitter()
  Object.assign(child, { exitCode: 0, signalCode: null, stdio: [{ closed: true, destroy() {} }], unref() {} })
  const tracker = trackSignedChildClose(child)
  const evidence = await observeSignedHandoff(handoffObservation({ child }))
  await assert.rejects(releaseExitedHandoffTransport(child, evidence, { timeoutMs: 15 }), /transport did not release/)
  assert.equal(tracker.observed, false)
  child.emit('close', 0, null)
  await releaseExitedHandoffTransport(child, evidence, { timeoutMs: 15 })
  assert.equal(tracker.observed, true, 'Already-observed close does not require a second close event')
})

test('handoff refuses a missing or duplicated fixed process even when all supplied rows exited', async () => {
  const states = observedProcesses()
  for (const rows of [states.slice(0, -1), [...states.slice(0, -1), states[2]]]) {
    await assert.rejects(observeSignedHandoff(handoffObservation({ snapshot: async () => rows })), /omitted or replaced/)
  }
})

function failedDriverFixture() {
  const child = new EventEmitter()
  Object.assign(child, { exitCode: 0, signalCode: null, stdio: [], unref() {} })
  trackSignedChildClose(child)
  child.emit('close', 0, null)
  const context = { child, processes: observedProcesses().map(({ exited: _exited, ...row }) => row) }
  const reports = []
  const diagnostics = []
  const options = {
    context, originalError: new Error('the original audit failure'),
    publish: async report => { reports.push(structuredClone(report)) },
    snapshot: async records => records.map(row => ({ ...row, exited: true })),
    refresh: async () => [], pause: async () => { await delay(1) }, emit: line => diagnostics.push(line),
  }
  return { child, context, reports, diagnostics, options }
}

test('failed audit remains resident for every live or unknown identity and then exits with its original failure', async () => {
  for (const code of ['EPERM', 'EACCES', 'UNKNOWN', null]) {
    const fixture = failedDriverFixture()
    let attempts = 0
    let releases = 0
    const result = await preserveFailedDriverUntilExit({
      ...fixture.options,
      snapshot: async records => {
        attempts += 1
        return records.map(row => row.role === 'electron' && attempts === 1
          ? processExitObservation(row, () => { if (code) throw Object.assign(new Error(code), { code }) }, () => null)
          : { ...row, exited: true })
      },
      pause: async () => { assert.equal(releases, 0); await delay(1) },
      release: async () => { releases += 1 },
    })
    assert.equal(attempts, 2)
    assert.equal(releases, 1)
    assert.equal(result.ok, false)
    assert.equal(result.handoffObserved, false)
    assert.equal(result.originalError, 'Error: the original audit failure')
    assert.equal(fixture.reports[0].operatorQuitRequired, true)
    assert.equal(result.stage, 'failed-after-operator-exit')
  }
})

test('failed wrapper exits, including nonzero and signal, can release transport without becoming successful handoffs', async () => {
  for (const state of [{ exitCode: 9, signalCode: null }, { exitCode: null, signalCode: 'SIGTERM' }]) {
    const fixture = failedDriverFixture()
    Object.assign(fixture.child, state)
    const result = await preserveFailedDriverUntilExit(fixture.options)
    assert.equal(result.wrapperExitCode, state.exitCode)
    assert.equal(result.wrapperSignalCode, state.signalCode)
    assert.equal(result.transportReleased, true)
    assert.equal(result.ok, false)
    assert.equal(result.handoffObserved, false)
  }
})

test('missing or duplicated failure snapshots do not discard fixed process identities', async () => {
  for (const malformed of [rows => rows.slice(0, -1), rows => [...rows.slice(0, -1), rows[2]]]) {
    const fixture = failedDriverFixture()
    let attempts = 0
    const result = await preserveFailedDriverUntilExit({
      ...fixture.options,
      snapshot: async records => {
        attempts += 1
        const rows = records.map(row => ({ ...row, exited: true }))
        return attempts === 1 ? malformed(rows) : rows
      },
    })
    assert.equal(attempts, 2)
    assert.equal(result.processes.length, fixture.context.processes.length)
    assert.match(result.observationError, /omitted or replaced/)
  }
})

test('query, ownership, evidence write and transport failures remain behind the failure fence', async () => {
  for (const failingOperation of ['snapshot', 'refresh', 'initial-publish', 'exited-publish', 'final-publish', 'release']) {
    const fixture = failedDriverFixture()
    let failed = false
    let pauses = 0
    let releaseCalls = 0
    const failOnce = operation => {
      if (!failed && operation === failingOperation) { failed = true; throw new Error(`injected ${operation}`) }
    }
    const result = await preserveFailedDriverUntilExit({
      ...fixture.options,
      publish: async report => {
        failOnce(report.stage === 'failed-awaiting-operator-quit' ? 'initial-publish' : report.stage === 'failed-processes-exited' ? 'exited-publish' : 'final-publish')
        await fixture.options.publish(report)
      },
      snapshot: async records => { failOnce('snapshot'); return fixture.options.snapshot(records) },
      refresh: async () => { failOnce('refresh'); return [] },
      release: async () => { releaseCalls += 1; failOnce('release') },
      pause: async () => { pauses += 1; await delay(1) },
    })
    assert.equal(failed, true, failingOperation)
    assert.ok(pauses >= 1, failingOperation)
    assert.ok(releaseCalls >= 1)
    assert.equal(result.originalError, 'Error: the original audit failure')
    assert.equal(result.ok, false)
    assert.equal(result.handoffObserved, false)
    assert.match(result.observationError, /injected/)
  }
})

test('failure evidence must persist before any process query or local pipe release', async () => {
  const fixture = failedDriverFixture()
  let writes = 0
  let queries = 0
  const result = await preserveFailedDriverUntilExit({
    ...fixture.options,
    publish: async report => { writes += 1; if (writes === 1) throw new Error('disk temporarily unavailable'); await fixture.options.publish(report) },
    snapshot: async records => { queries += 1; assert.ok(writes >= 2); return fixture.options.snapshot(records) },
    pause: async () => { assert.equal(queries, 0); await delay(1) },
  })
  assert.equal(result.handoffObserved, false)
  assert.equal(queries, 1)
})

test('a recovered Gateway reusing a PID is appended and must exit independently from the original identity', async () => {
  const fixture = failedDriverFixture()
  const original = fixture.context.processes.find(row => row.role === 'gateway')
  const recovered = { ...original, startIdentity: 'windows-creation-filetime:9000' }
  let attempts = 0
  let releases = 0
  const result = await preserveFailedDriverUntilExit({
    ...fixture.options,
    refresh: async () => [recovered],
    snapshot: async records => {
      attempts += 1
      return records.map(row => row.pid === recovered.pid
        ? processExitObservation(row, () => { if (attempts > 1) throw Object.assign(new Error('gone'), { code: 'ESRCH' }) }, () => recovered.startIdentity)
        : { ...row, exited: true })
    },
    pause: async () => { assert.equal(releases, 0); await delay(1) },
    release: async () => { releases += 1 },
  })
  assert.equal(attempts, 2)
  assert.equal(result.processes.length, fixture.context.processes.length + 1)
  assert.deepEqual(result.processes.filter(row => row.pid === recovered.pid).map(row => row.startIdentity), [original.startIdentity, recovered.startIdentity])
  assert.equal(releases, 1)
})

test('the process snapshot queries each reused PID once while retaining both captured identities', async () => {
  const records = observedProcesses()
  const original = records.find(row => row.role === 'gateway')
  const recovered = { ...original, startIdentity: 'windows-creation-filetime:9000' }
  let queries = 0
  const states = await signedProcessSnapshot([...records, recovered], { timeoutMs: 1000 }, {
    probePid: () => {},
    queryIdentities: async pids => {
      queries += 1
      assert.equal(pids.length, 4)
      assert.equal(new Set(pids).size, pids.length)
      return new Map(records.map(row => [row.pid, row.pid === original.pid ? recovered.startIdentity : row.startIdentity]))
    },
  })
  assert.equal(queries, 1)
  const reused = states.filter(row => row.pid === recovered.pid)
  assert.equal(reused.length, 2)
  assert.equal(reused[0].exited, true)
  assert.equal(reused[1].exited, false)
})

test('late handoff report or transport errors retain the fixed context even after the app reference is gone', async () => {
  for (const originalError of [new Error('handoff evidence write failed'), new Error('handoff transport release timed out')]) {
    const fixture = failedDriverFixture()
    const result = await preserveFailedDriverUntilExit({ ...fixture.options, originalError })
    assert.equal(result.originalError, String(originalError))
    assert.equal(result.ok, false)
    assert.equal(result.handoffObserved, false)
    assert.equal(result.releaseGatePassed, false)
    assert.equal(result.transportReleased, true)
  }
})

test('missing original identities and unpinned recovery stay resident for operator diagnosis', { timeout: 10_000 }, async () => {
  const observerUrl = new URL('./fixtures/packaged-cached-handoff/signed-exit-observer.mjs', import.meta.url).href
  for (const missingContext of [true, false]) {
    // Only this inert subprocess self-exits after checking continued residence.
    // The production fence has no cancellation/kill escape for missing identity.
    const code = `import {preserveFailedDriverUntilExit} from ${JSON.stringify(observerUrl)};
      const rows = ['electron','wrapper','gateway'].map((role,index)=>({pid:100+index,role,startIdentity:'windows-creation-filetime:'+index}));
      let pauses=0; let latest; let settled=false;
      preserveFailedDriverUntilExit({context:${missingContext ? 'null' : '{child:{exitCode:0,signalCode:null},processes:rows}'},originalError:new Error('fixed failure'),
        publish:async report=>{latest=report},emit:()=>{},snapshot:async records=>records.map(row=>({...row,exited:true})),
        refresh:async()=>{throw Object.assign(new Error('unpinned recovery'),{unresolvedProcessIdentity:true})},
        release:async()=>{throw new Error('must never release')},pause:async()=>{pauses++;await new Promise(r=>setTimeout(r,5));if(pauses===3){process.stdout.write(JSON.stringify({settled,latest,pauses}));process.exit(0)}}
      }).then(()=>{settled=true},()=>{settled=true});`
    const child = spawn(process.execPath, ['--input-type=module', '-e', code], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', data => { stdout += data })
    child.stderr.on('data', data => { stderr += data })
    await once(child, 'close')
    assert.equal(child.exitCode, 0, stderr)
    const result = JSON.parse(stdout)
    assert.equal(result.settled, false)
    assert.equal(result.pauses, 3)
    assert.equal(result.latest.operatorQuitRequired, true)
    assert.equal(result.latest.manualDiagnosisRequired, true)
    assert.equal(result.latest.automaticReleaseAvailable, false)
    assert.equal(result.latest.handoffObserved, false)
    assert.equal(result.latest.originalError, 'Error: fixed failure')
  }
})
