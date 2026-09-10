import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { captureElectronProcessIdentity } from '../../packaged-first-send-cleanup.mjs'
import { desktopProfileFingerprint, loadDesktopGatewayOwnershipRecord, verifyDesktopGatewayOwnership } from '../../../dist/desktop-gateway-ownership.js'

const comparableStart = /^windows-creation-filetime:\d+$/
const childCloseObservations = new WeakMap()

export function trackSignedChildClose(child) {
  if (!childCloseObservations.has(child)) {
    const tracker = { observed: false }
    tracker.promise = new Promise(resolveClose => child.once('close', () => {
      tracker.observed = true
      resolveClose()
    }))
    childCloseObservations.set(child, tracker)
  }
  return childCloseObservations.get(child)
}
const processStartScript = `
$ErrorActionPreference = 'Stop'
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$rows = @()
foreach ($processId in $request.pids) {
  $identity = $null
  try { $identity = 'windows-creation-filetime:' + [Diagnostics.Process]::GetProcessById([int]$processId).StartTime.ToFileTime() } catch {}
  $rows += @{ pid = [int]$processId; startIdentity = $identity }
}
ConvertTo-Json -InputObject @($rows) -Compress
`

function parseRecords(text) {
  return text.split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line))
}

export function signedHandoffLogEvidence(checkpoint, current, version) {
  assert.ok(current.startsWith(checkpoint), 'The Desktop log was replaced or truncated after the install click')
  const suffix = current.slice(checkpoint.length)
  // A concurrent final JSON line is not evidence until its newline is written.
  const complete = suffix.slice(0, suffix.lastIndexOf('\n') + 1)
  const phases = [
    ['running', 'deferred', 'verifying Windows update before installation'],
    ['deferred', 'draining', 'stopping Gateway for Windows installer'],
    ['draining', 'committed', 'Windows installer process started'],
  ]
  let phase = 0
  let handoff = null
  for (const record of parseRecords(complete)) {
    if (record.event === 'desktop_exit_phase') {
      assert.ok(!handoff && phase < phases.length && record.from === phases[phase][0] && record.to === phases[phase][1] && record.reason === phases[phase][2], 'The install attempt changed lifecycle, recovered, or committed out of order')
      phase += 1
    }
    if (record.event === 'update_windows_installer_handoff') {
      assert.ok(phase === phases.length && !handoff && record.version === version && record.tag === `v${version}`, 'The handoff log does not uniquely identify this candidate and attempt')
      handoff = record
    }
    assert.notEqual(record.event, 'gateway_spawned', 'A new Gateway appeared after the handoff snapshot')
  }
  return { committedExitLogged: phase === phases.length, handoffLogged: handoff !== null, handoffAt: handoff?.at ?? null }
}

export function processExitObservation(record, probePid, readIdentity) {
  assert.ok(Number.isSafeInteger(record.pid) && record.pid > 0 && comparableStart.test(record.startIdentity), 'A comparable pre-click process identity is required')
  try { probePid(record.pid) } catch (error) {
    return { ...record, exited: error?.code === 'ESRCH', pidPresent: error?.code !== 'ESRCH', probeError: error?.code ?? String(error), liveStartIdentity: null, recycled: false }
  }
  const liveStartIdentity = readIdentity(record.pid)
  const recycled = comparableStart.test(liveStartIdentity ?? '') && liveStartIdentity !== record.startIdentity
  return { ...record, exited: recycled, pidPresent: true, probeError: null, liveStartIdentity, recycled }
}

function assertCompleteSnapshot(records, states) {
  const key = row => `${row.pid}/${row.startIdentity}/${row.role}`
  assert.ok(states.length === records.length && new Set(states.map(key)).size === records.length && records.every(record => states.some(row => key(row) === key(record))), 'A process snapshot omitted or replaced a fixed process identity')
}

async function readProcessIdentities(pids, { signal, timeoutMs }) {
  assert.ok(pids.length > 0 && pids.length <= 4 && pids.every(pid => Number.isSafeInteger(pid) && pid > 0))
  return await new Promise((resolveRows, reject) => {
    const child = execFile(join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'), [
      '-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(processStartScript, 'utf16le').toString('base64'),
    ], { windowsHide: true, timeout: Math.max(1, Math.min(5000, timeoutMs)), signal, encoding: 'utf8', maxBuffer: 16 * 1024 }, (error, stdout) => {
      if (error) return reject(error)
      try {
        const rows = JSON.parse(stdout.replace(/^\uFEFF/, ''))
        assert.ok(Array.isArray(rows) && rows.length === pids.length)
        const identities = new Map()
        for (const row of rows) {
          assert.ok(pids.includes(row.pid) && !identities.has(row.pid))
          assert.ok(row.startIdentity === null || comparableStart.test(row.startIdentity))
          identities.set(row.pid, row.startIdentity)
        }
        resolveRows(identities)
      } catch (error) { reject(error) }
    })
    child.stdin.on('error', () => {})
    child.stdin.end(JSON.stringify({ pids }))
  })
}

export async function signedProcessSnapshot(records, options, { probePid = pid => process.kill(pid, 0), queryIdentities = readProcessIdentities } = {}) {
  const present = records.filter(record => {
    try { probePid(record.pid); return true } catch { return false }
  })
  const pids = [...new Set(present.map(record => record.pid))]
  const identities = new Map()
  const deadline = Date.now() + options.timeoutMs
  for (let index = 0; index < pids.length; index += 4) {
    if (Date.now() >= deadline) throw new Error('Failure process snapshot exceeded its query budget')
    for (const [pid, value] of await queryIdentities(pids.slice(index, index + 4), { ...options, timeoutMs: deadline - Date.now() })) identities.set(pid, value)
  }
  // Reprobe after the asynchronous query: a process can have exited while it ran.
  return records.map(record => processExitObservation(record, probePid, pid => identities.get(pid) ?? null))
}

export async function captureSignedHandoffProcesses({ app, userDataDir, desktopLogPath, launchCheckpoint, baselineVersion }) {
  assert.equal(process.platform, 'win32')
  const identity = await captureElectronProcessIdentity(app)
  assert.ok(identity.electronPid && identity.wrapperPid, 'The actual Electron and wrapper PIDs must be captured while A is live')
  assert.notEqual(identity.electronPid, identity.wrapperPid, 'This Windows observer requires distinct Playwright wrapper and actual Electron processes')
  const loaded = loadDesktopGatewayOwnershipRecord(resolve(userDataDir, 'gateway-ownership', desktopProfileFingerprint(resolve(userDataDir, 'opensquilla'))))
  assert.equal(loaded.status, 'valid', 'A must have a verifiable synthetic-profile Gateway')
  assert.equal(loaded.record.version, baselineVersion)
  assert.equal(await verifyDesktopGatewayOwnership(loaded.record), true)
  const log = await readFile(desktopLogPath, 'utf8')
  assert.ok(log.startsWith(launchCheckpoint))
  const launches = parseRecords(log.slice(launchCheckpoint.length)).filter(row => row.event === 'gateway_spawned')
  // This native cell has one fresh synthetic profile and no profile switching.
  // Fail rather than silently omit a restarted/additional lifecycle-owned child.
  assert.equal(launches.length, 1, 'The synthetic A process must have exactly one observed Gateway launch')
  for (const pid of [loaded.record.pid, launches[0].pid]) {
    assert.ok(Number.isSafeInteger(pid) && pid > 0 && pid !== identity.electronPid && pid !== identity.wrapperPid, 'Gateway identity must be distinct from A and its wrapper')
  }
  const roles = new Map([[identity.wrapperPid, 'wrapper'], [identity.electronPid, 'electron'], [loaded.record.pid, 'gateway']])
  if (!roles.has(launches[0].pid)) roles.set(launches[0].pid, 'gateway-launcher')
  const identities = await readProcessIdentities([...roles.keys()], { timeoutMs: 5000 })
  const processes = [...roles].map(([pid, role]) => ({ pid, role, startIdentity: identities.get(pid) }))
  for (const record of processes) assert.ok(comparableStart.test(record.startIdentity ?? ''), `Cannot establish the ${record.role} start identity`)
  const gateway = processes.find(record => record.pid === loaded.record.pid)
  assert.equal(gateway.startIdentity, loaded.record.start_identity, 'Gateway record and process must use the same creation-time identity')
  return { processes, checkpoint: log, child: app.process(), electronPid: identity.electronPid, userDataDir, desktopLogPath, baselineVersion }
}

export async function observeSignedHandoff({ processes, child, checkpoint, expectedVersion, readLog, clickPromise = Promise.resolve(), snapshot = signedProcessSnapshot, timeoutMs = 180_000, intervalMs = 250 }) {
  const controller = new AbortController()
  const deadline = Date.now() + timeoutMs
  let latest = null
  try {
    const observation = (async () => {
      while (!controller.signal.aborted) {
        if (child.signalCode !== null || (child.exitCode !== null && child.exitCode !== 0)) throw new Error('The observed A wrapper did not exit naturally with code zero')
        const states = await snapshot(processes, { signal: controller.signal, timeoutMs: Math.max(1, deadline - Date.now()) })
        assertCompleteSnapshot(processes, states)
        const logs = signedHandoffLogEvidence(checkpoint, await readLog(), expectedVersion)
        const electronExited = states.some(row => row.role === 'electron' && row.exited === true)
        const wrapperExited = states.some(row => row.role === 'wrapper' && row.exited === true) && child.exitCode === 0 && child.signalCode === null
        const gateways = states.filter(row => row.role === 'gateway' || row.role === 'gateway-launcher')
        const allGatewayExited = gateways.length > 0 && gateways.every(row => row.exited === true)
        latest = { electronExited, wrapperExited, allGatewayExited, ...logs, processes: states, wrapperExitCode: child.exitCode, wrapperSignalCode: child.signalCode }
        if (electronExited && wrapperExited && allGatewayExited && logs.committedExitLogged && logs.handoffLogged) return latest
        await delay(intervalMs, undefined, { signal: controller.signal })
      }
      throw new Error('Signed handoff observation was aborted')
    })()
    return await Promise.race([
      Promise.all([observation, clickPromise]).then(([evidence]) => evidence),
      delay(timeoutMs, undefined, { signal: controller.signal }).then(() => { throw new Error(`Signed handoff identity/log observation timed out: ${JSON.stringify(latest)}`) }),
    ])
  } finally { controller.abort() }
}

export async function releaseExitedHandoffTransport(child, evidence, { timeoutMs = 5000 } = {}) {
  assert.ok(evidence?.electronExited && evidence.wrapperExited && evidence.allGatewayExited && evidence.committedExitLogged && evidence.handoffLogged, 'Persist independently verified handoff evidence before releasing transport')
  assert.equal(child.exitCode, 0)
  assert.equal(child.signalCode, null)
  await releaseObservedExitedTransport(child, timeoutMs)
}

async function releaseObservedExitedTransport(child, timeoutMs) {
  assert.ok(child.exitCode !== null || child.signalCode !== null, 'Cannot release transport while the wrapper is still running')
  const tracker = childCloseObservations.get(child)
  assert.ok(tracker, 'A child.close tracker must be installed while the managed app is live')
  const controller = new AbortController()
  try {
    // The tracker starts immediately after launch, after Playwright installs
    // its listener. Stream closure alone cannot prove its exit hook was removed.
    const streams = (child.stdio ?? []).filter(Boolean)
    for (const stream of streams) stream.destroy()
    child.unref()
    await Promise.race([tracker.promise, delay(timeoutMs, undefined, { signal: controller.signal }).then(() => { throw new Error('Exited Electron transport did not release within its deadline') })])
    assert.equal(tracker.observed, true)
  } finally { controller.abort() }
}

async function refreshFailureGateway(context, records) {
  // A rejected handoff can recover its owned Gateway. Retain the original
  // records and add only a currently authenticated, same-profile A identity.
  const loaded = loadDesktopGatewayOwnershipRecord(resolve(context.userDataDir, 'gateway-ownership', desktopProfileFingerprint(resolve(context.userDataDir, 'opensquilla'))))
  if (loaded.status === 'invalid') throw new Error('Gateway ownership is unreadable during the failed audit')
  if (loaded.status !== 'valid' || loaded.record.version !== context.baselineVersion) return []
  const record = loaded.record
  if (records.some(row => row.pid === record.pid && row.startIdentity === record.start_identity)) return []
  try { process.kill(record.pid, 0) } catch (error) { if (error.code === 'ESRCH') return []; throw error }
  assert.ok(comparableStart.test(record.start_identity) && await verifyDesktopGatewayOwnership(record), 'A recovered Gateway requires ownership proof before tracking')
  try {
    const log = await readFile(context.desktopLogPath, 'utf8')
    if (!log.startsWith(context.checkpoint)) throw Object.assign(new Error('Cannot associate recovery with a replaced Desktop log'), { unresolvedProcessIdentity: true })
    const launches = parseRecords(log.slice(context.checkpoint.length)).filter(row => row.event === 'gateway_spawned')
    if (launches.length !== 1) throw Object.assign(new Error('A recovered Gateway needs exactly one new launch record; uncertain recovery stays resident'), { unresolvedProcessIdentity: true })
    const launcherPid = launches[0].pid
    if (!Number.isSafeInteger(launcherPid) || launcherPid <= 0) throw Object.assign(new Error('Recovered Gateway launcher PID is invalid'), { unresolvedProcessIdentity: true })
    const added = [{ pid: record.pid, role: 'gateway', startIdentity: record.start_identity }]
    if (launcherPid !== record.pid) {
      // A post-failure PID query cannot retroactively identify a launcher that
      // may already have exited/recycled. Keep the failure fence closed instead.
      throw Object.assign(new Error('Recovered Gateway has an unpinned launcher; operator identity diagnosis is required'), { unresolvedProcessIdentity: true })
    }
    return added
  } catch (error) {
    // Once a new authenticated Gateway exists, losing its historical launcher
    // association cannot be repaired by a later missing ownership file.
    error.unresolvedProcessIdentity = true
    throw error
  }
}

export async function preserveFailedDriverUntilExit({ context, originalError, publish, emit = line => console.error(line), snapshot = signedProcessSnapshot, refresh = refreshFailureGateway, intervalMs = 5000, pause = () => delay(intervalMs), release = releaseObservedExitedTransport }) {
  const originalFailure = String(originalError)
  // Even a pending filesystem/report promise must not let Node exit and run
  // Playwright's force-exit hook. This ref is cleared only after safe release.
  const keepAlive = setInterval(() => {}, 5000)
  const records = context?.processes ? context.processes.map(row => ({ ...row })) : []
  let published = false
  let lastDiagnostic = null
  let unresolvedProcessIdentity = false
  const status = {
    ok: false, stage: 'failed-awaiting-operator-quit', originalError: originalFailure,
    operatorQuitRequired: true, handoffObserved: false, releaseGatePassed: false,
    instruction: 'The audit failed. Do not retry or start another installer. Quit the remaining A client normally; this driver stays resident until its recorded processes exit.',
    processes: records, allRecordedProcessesExited: false, transportReleased: false,
  }
  const hasIdentityContext = context?.child && records.length >= 3 && ['electron', 'wrapper', 'gateway'].every(role => records.some(row => row.role === role)) && records.every(row => Number.isSafeInteger(row.pid) && row.pid > 0 && comparableStart.test(row.startIdentity ?? ''))
  if (!hasIdentityContext) {
    status.manualDiagnosisRequired = true
    status.automaticReleaseAvailable = false
    status.instruction = 'The audit failed before complete process identities were captured. Do not close this driver terminal or retry. An operator must diagnose the missing identities; ordinary Quit alone cannot automatically release this safety fence.'
  }
  try { emit(`${originalFailure}. ${status.instruction}`) } catch {}
  // No timeout may unwind this failure fence into Playwright's process-exit
  // hook while an original process or inherited transport is still live.
  for (;;) {
    try {
      // Persist failure before querying or releasing anything. Retry report
      // failures without letting a disk error take the live client down.
      await publish({ ...status, observedAt: new Date().toISOString() })
      published = true
      assert.ok(hasIdentityContext, 'The failed audit lacks complete pre-operation process identities; retain it for operator diagnosis')
      assert.equal(unresolvedProcessIdentity, false, 'An unpinned recovery process requires operator diagnosis; later disappearance cannot repair missing identity evidence')
      const added = await refresh(context, records)
      for (const record of added) {
        assert.ok(Number.isSafeInteger(record.pid) && record.pid > 0 && comparableStart.test(record.startIdentity) && ['gateway', 'gateway-launcher'].includes(record.role))
        if (!records.some(row => row.pid === record.pid && row.startIdentity === record.startIdentity)) records.push({ ...record })
      }
      const controller = new AbortController()
      let states
      try {
        states = await Promise.race([
          snapshot(records, { signal: controller.signal, timeoutMs: 5000 }),
          delay(5000, undefined, { signal: controller.signal }).then(() => { throw new Error('Failed-audit process query timed out') }),
        ])
      } finally { controller.abort() }
      assertCompleteSnapshot(records, states)
      status.processes = states
      status.allRecordedProcessesExited = states.every(row => row.exited === true)
      const wrapperEnded = context.child.exitCode !== null || context.child.signalCode !== null
      if (published && status.allRecordedProcessesExited && wrapperEnded) {
        status.stage = 'failed-processes-exited'
        status.wrapperExitCode = context.child.exitCode
        status.wrapperSignalCode = context.child.signalCode
        await publish({ ...status, observedAt: new Date().toISOString() })
        // Nonzero/externally signalled exits remain failures. Nothing in this
        // branch creates handoff evidence or signals any OS process.
        await release(context.child, 5000)
        status.transportReleased = true
        status.operatorQuitRequired = false
        status.stage = 'failed-after-operator-exit'
        await publish({ ...status, observedAt: new Date().toISOString() })
        clearInterval(keepAlive)
        return status
      }
    } catch (error) {
      if (error.unresolvedProcessIdentity) {
        unresolvedProcessIdentity = true
        status.manualDiagnosisRequired = true
        status.automaticReleaseAvailable = false
        status.instruction = 'An unpinned recovery process was observed. Do not close this driver terminal or retry. Operator identity diagnosis is required; this fence cannot automatically release from later PID observations.'
      }
      status.observationError = String(error)
      status.operatorQuitRequired = true
      if (status.observationError !== lastDiagnostic) {
        try { emit(`Failed audit remains resident: ${originalFailure}. Observation: ${status.observationError}`) } catch {}
        lastDiagnostic = status.observationError
      }
    }
    // Ref'ed, low-frequency timer deliberately keeps the driver alive. Tests
    // inject a deterministic pause; native callers use the fixed five seconds.
    try { await pause() } catch (error) {
      try { emit(`Failed-audit pause error: ${String(error)}`) } catch {}
      await delay(5000)
    }
  }
}
