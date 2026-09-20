import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'

import {
  DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS,
  lifecycleAllowsProcessSpawn,
  stopAndJoinLifecycleProcesses,
  waitForGatewayReadiness,
} from '../dist/gateway-lifecycle.js'

// Run the actual main-process startup wiring with profile inspection held open.
// Readiness helpers alone do not cover the descriptor seen by the first renderer.
const main = readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8')
function mainSection(start, end) {
  const from = main.indexOf(start)
  assert.notEqual(from, -1, start)
  const to = main.indexOf(end, from)
  assert.notEqual(to, -1, end)
  return main.slice(from, to)
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}

function mainStartupHarness() {
  const inspection = deferred()
  const inspectionStarted = deferred()
  const calls = { rendered: [], published: [], readiness: [], starts: 0, successes: 0, failures: [] }
  const snapshot = () => runInContext('desktopGatewayConnectionSnapshot()', context)
  const context = createContext({
    Error,
    isQuitting: false,
    gatewayProcess: null,
    gatewayProfileKey: 'synthetic-profile',
    forceOnboardingOnNextStartup: false,
    activeDesktopProfile: () => ({ home: 'synthetic-profile' }),
    desktopProfileFingerprint: () => 'synthetic-fingerprint',
    desktopProfileKey: () => 'synthetic-profile',
    cancelGatewayUnexpectedExitRestart() {},
    createMainWindow: async () => { calls.rendered.push(snapshot()) },
    focusMainWindow() {},
    inspectActiveProfileBeforeStartup: () => {
      inspectionStarted.resolve()
      return inspection.promise
    },
    syncDesktopConsentMirror: async () => {},
    desktopTelemetryRuntimeGate: { close() {} },
    desktopLog() {},
    loadDesktopRendererIntoCurrentWindow: async () => { calls.rendered.push(snapshot()) },
    beginGatewayStartTelemetry() {},
    readinessCheck: async (url) => {
      calls.readiness.push(url)
      return true
    },
    ensureGatewayStarted: async () => {
      calls.starts += 1
      throw new Error('Unexpected Gateway start')
    },
    publishGatewayConnection: () => { calls.published.push(snapshot()) },
    sendBootStatus() {},
    finishAppStartSuccess: () => { calls.successes += 1 },
    finishAppStartFailure: (error) => { calls.failures.push(error.message) },
    currentMainWindow: () => null,
  })
  runInContext([
    mainSection('let desktopOpenFlowRevision =', 'function beginDesktopWriterOperation('),
    mainSection('const gatewayState =', 'let sandboxUpgradeRefreshInFlight ='),
    mainSection('function transitionGatewayConnection(', 'const artifactPreviewLeaseBroker ='),
    mainSection('async function reuseHealthyGatewayState(', 'async function verifyOwnedGatewayLaunch('),
    mainSection('async function openOrResumeDesktopApp(', '// SIGKILL deadline'),
  ].join('\n'), context)
  return { context, calls, snapshot, inspection, inspectionStarted: inspectionStarted.promise }
}

async function runColdStartDescriptorCase() {
  const harness = mainStartupHarness()
  const opening = runInContext('openOrResumeDesktopApp()', harness.context)
  await harness.inspectionStarted

  for (const descriptor of [...harness.calls.rendered, harness.snapshot()]) {
    assert.equal(descriptor.status, 'starting', 'profile preflight is part of startup')
    assert.equal(descriptor.wsUrl, null, 'startup must not grant WebSocket access before readiness')
    assert.equal(descriptor.authToken, null)
    assert.equal(descriptor.error, null)
  }
  assert.equal(harness.calls.rendered.length, 1, 'renderer loads before profile preflight completes')

  harness.inspection.reject(new Error('Synthetic profile inspection failure'))
  await opening
  assert.equal(harness.snapshot().status, 'error', 'real startup failures remain actionable')
  assert.equal(harness.calls.published.at(-1).error, 'Synthetic profile inspection failure')
  assert.deepEqual(harness.calls.failures, ['Synthetic profile inspection failure'])
}

async function runWarmExternalGatewayReuseCase() {
  const harness = mainStartupHarness()
  const url = 'http://127.0.0.1:8765'
  runInContext(`Object.assign(gatewayState, { status: 'ready', url: '${url}', port: 8765 })`, harness.context)
  const opening = runInContext('openOrResumeDesktopApp()', harness.context)
  await harness.inspectionStarted
  assert.equal(harness.snapshot().status, 'ready', 'warm profile preflight preserves the healthy connection')
  harness.inspection.resolve(true)
  await opening

  assert.deepEqual(harness.calls.readiness, [url], 'warm launch validates and reuses the external Gateway')
  assert.equal(harness.calls.starts, 0)
  assert.equal(harness.calls.successes, 1)
  assert.deepEqual(harness.calls.failures, [])
  for (const descriptor of [...harness.calls.rendered, ...harness.calls.published]) {
    assert.equal(descriptor.status, 'ready', 'warm launch must not publish a spurious startup transition')
  }
}

function fakeClock() {
  let current = 0
  return {
    now: () => current,
    advance: (milliseconds) => {
      current += milliseconds
    },
    sleep: async (milliseconds) => {
      current += milliseconds
    },
  }
}

async function runReadinessBeforePrimaryDeadlineCase() {
  const clock = fakeClock()
  let probes = 0
  const result = await waitForGatewayReadiness({
    probe: async () => ++probes === 2,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'ready', late: false })
  assert.equal(probes, 2)
}

async function runLateReadinessCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async () => clock.now() >= 15,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'ready', late: true })
  assert.equal(clock.now(), 15)
}

async function runReadinessTimeoutCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async () => false,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 6,
    ...clock,
  })

  assert.deepEqual(result, { status: 'timeout' })
  assert.equal(clock.now(), 20)
}

async function runReadinessExitCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async () => false,
    exitMessage: () => clock.now() >= 5 ? 'gateway exited' : null,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'exited', message: 'gateway exited' })
  assert.equal(clock.now(), 5)
}

async function runReadinessExitDuringSuccessfulProbeCase() {
  let exited = false
  const result = await waitForGatewayReadiness({
    probe: async () => {
      exited = true
      return true
    },
    exitMessage: () => exited ? 'gateway exited during probe' : null,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
  })

  assert.deepEqual(result, { status: 'exited', message: 'gateway exited during probe' })
}

async function runProbeCrossesDeadlineCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async (remainingMs) => {
      clock.advance(remainingMs + 1)
      return true
    },
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'timeout' })
  assert.equal(clock.now(), 21, 'readiness after the hard deadline is rejected')
}

async function runNeverResolvingProbeCase() {
  const startedAt = performance.now()
  const result = await waitForGatewayReadiness({
    probe: async () => await new Promise(() => {}),
    primaryTimeoutMs: 5,
    lateGraceMs: 10,
    pollIntervalMs: 2,
  })

  assert.deepEqual(result, { status: 'timeout' })
  assert.ok(performance.now() - startedAt < 250, 'a stuck probe cannot escape the hard budget')
}

async function runStoppingSetOnlyCase() {
  const stopping = { name: 'already-stopping', live: true }
  const stopped = []
  const joined = []

  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: (process) => stopped.push(process.name),
    liveProcesses: () => stopping.live ? [stopping] : [],
    waitForExit: async (process) => {
      joined.push(process.name)
      process.live = false
      return true
    },
  })

  assert.equal(exited, true)
  assert.deepEqual(stopped, [])
  assert.deepEqual(joined, ['already-stopping'])
}

async function runCurrentPlusStoppingCase() {
  const current = { name: 'current', live: true }
  const stopping = { name: 'already-stopping', live: true }
  let currentSlot = current
  const joined = []

  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => currentSlot,
    stopCurrentProcess: (process) => {
      assert.equal(process, current)
      currentSlot = null
    },
    liveProcesses: () => [current, stopping].filter((process) => process.live),
    waitForExit: async (process) => {
      joined.push(process.name)
      process.live = false
      return true
    },
  })

  assert.equal(exited, true)
  assert.deepEqual(new Set(joined), new Set(['current', 'already-stopping']))
}

async function runLatePublishedChildCase() {
  const first = { name: 'first', live: true }
  const late = { name: 'late', live: false }
  const joined = []

  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: () => assert.fail('there is no current process'),
    liveProcesses: () => [first, late].filter((process) => process.live),
    waitForExit: async (process) => {
      joined.push(process.name)
      process.live = false
      if (process === first) late.live = true
      return true
    },
  })

  assert.equal(exited, true)
  assert.deepEqual(joined, ['first', 'late'])
}

async function runFailClosedCase() {
  const stuck = { name: 'stuck', live: true }
  let handoff = false
  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: () => {},
    liveProcesses: () => [stuck],
    waitForExit: async () => false,
  })
  if (exited) handoff = true

  assert.equal(exited, false)
  assert.equal(handoff, false)
}

async function runPendingSpawnAdmissionCase() {
  let lifecycleClosing = false
  let writerAdmissionClosed = false
  let published = false

  const pendingStart = Promise.resolve().then(() => {
    if (lifecycleAllowsProcessSpawn(lifecycleClosing, writerAdmissionClosed)) {
      published = true
    }
  })

  // The lifecycle closes admission before checking the (still empty) published
  // set. When the pending start resumes, its final pre-spawn check must reject
  // publication even though there was no ChildProcess handle to join.
  lifecycleClosing = true
  writerAdmissionClosed = true
  assert.equal(await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: () => {},
    liveProcesses: () => [],
    waitForExit: async () => true,
  }), true)
  await pendingStart

  assert.equal(lifecycleAllowsProcessSpawn(true, false), false)
  assert.equal(lifecycleAllowsProcessSpawn(false, true), false)
  assert.equal(lifecycleAllowsProcessSpawn(false, false, 1), false)
  assert.equal(lifecycleAllowsProcessSpawn(false, false, 0), true)
  assert.equal(published, false)
}

async function runSlowColdStartReadinessCase() {
  const clock = fakeClock()
  let probes = 0
  const result = await waitForGatewayReadiness({
    probe: async () => {
      probes += 1
      return clock.now() >= 60_000
    },
    primaryTimeoutMs: 45_000,
    lateGraceMs: DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS - 45_000,
    pollIntervalMs: 500,
    ...clock,
  })

  assert.deepEqual(result, { status: 'ready', late: true })
  assert.equal(DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS, 120_000)
  assert.equal(clock.now(), 60_000, 'a healthy cold start must survive the former deadline')
  assert.ok(probes > 1)
}

await runColdStartDescriptorCase()
await runWarmExternalGatewayReuseCase()
await runStoppingSetOnlyCase()
await runCurrentPlusStoppingCase()
await runLatePublishedChildCase()
await runFailClosedCase()
await runPendingSpawnAdmissionCase()
await runReadinessBeforePrimaryDeadlineCase()
await runLateReadinessCase()
await runReadinessTimeoutCase()
await runReadinessExitCase()
await runReadinessExitDuringSuccessfulProbeCase()
await runProbeCrossesDeadlineCase()
await runNeverResolvingProbeCase()
await runSlowColdStartReadinessCase()

console.log('desktop gateway lifecycle tests passed')
