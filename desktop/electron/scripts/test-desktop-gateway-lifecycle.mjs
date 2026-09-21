import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'

import {
  DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS,
  lifecycleAllowsProcessSpawn,
  stopAndJoinLifecycleProcesses,
  waitForGatewayReadiness,
} from '../dist/gateway-lifecycle.js'
import { DesktopRoutingConfigurationError } from '../dist/desktop-router-profiles.js'

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
  const calls = { rendered: [], published: [], readiness: [], starts: 0, successes: 0, failures: [], invitations: 0 }
  const snapshot = () => runInContext('desktopGatewayConnectionSnapshot()', context)
  const context = createContext({
    Error,
    DesktopRoutingConfigurationError,
    isQuitting: false,
    appExitPhase: 'running',
    gatewayProcess: null,
    gatewayProfileKey: 'synthetic-profile',
    forceOnboardingOnNextStartup: false,
    onboardingPromptProfileKey: null,
    onboardingFlows: { active: null },
    runOnboarding: () => {
      calls.invitations += 1
      return new Promise(() => {})
    },
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

async function runOptionalOnboardingDoesNotDelayReadyCase() {
  const harness = mainStartupHarness()
  runInContext(`
    Object.assign(gatewayState, { status: 'ready', url: 'http://127.0.0.1:8765', port: 8765 });
    onboardingPromptProfileKey = 'synthetic-profile';
  `, harness.context)
  const opening = runInContext('openOrResumeDesktopApp()', harness.context)
  await harness.inspectionStarted
  harness.inspection.resolve(true)
  await opening

  assert.equal(harness.calls.invitations, 1, 'ready startup offers the optional first-run invitation')
  assert.equal(harness.calls.successes, 1, 'startup completes while the invitation remains unanswered')
  assert.equal(harness.snapshot().status, 'ready')
  assert.deepEqual(harness.calls.failures, [])
  assert.equal(runInContext('onboardingPromptProfileKey', harness.context), null)
}

function mainExitHarness() {
  const child = { pid: 1234 }
  const previewCleanup = deferred()
  const drainRequested = deferred()
  const drainResult = deferred()
  const calls = { published: [], order: [], refreshes: 0, exits: [], errors: [], resumedQuits: 0 }
  let beforeQuit
  const context = createContext({
    appExitPhase: 'running', isQuitting: false, process: { platform: 'win32' },
    gatewayProcess: child,
    gatewayProcessOwnershipContexts: new Map([[child, { nonce: 'synthetic-owner' }]]),
    desktopGatewayAuthToken: nonce => `auth-${nonce}`,
    activeDesktopProfile: () => ({ home: 'synthetic-profile' }),
    desktopProfileFingerprint: () => 'synthetic-fingerprint',
    currentMainWindow: () => ({ webContents: {
      getURL: () => 'opensquilla-app://desktop/chat',
      send: (channel, descriptor) => {
        assert.equal(channel, 'gateway:connection-changed')
        calls.published.push(descriptor)
        calls.order.push(`descriptor:${descriptor.status}`)
      },
    } }),
    isDesktopRendererDocumentUrl: () => true,
    refreshSandboxUpgradeReport: () => { calls.refreshes += 1 },
    desktopLog() {}, rebuildWindowsTrayMenu() {},
    app: {
      on: (event, handler) => { assert.equal(event, 'before-quit'); beforeQuit = handler },
      exit: code => calls.exits.push(code),
      quit: () => { calls.resumedQuits += 1 },
    },
    desktopUpdateCheckScheduler: { stop() {} },
    systemSessionEnding: false, updateApplying: false, updateInstallHandoffReady: false,
    quitRequestedDuringUpdateDrain: false, quitGatewayDrainPromise: null,
    quitDeferredForDesktopWriters: false, quitWriterAdmission: null,
    desktopWriters: { activeCount: 0, close: () => Symbol('quit'), reopen() {} },
    desktopReliabilityTelemetry: { finishSession() {} },
    artifactPreviewLeaseBroker: {
      clear() {},
      revokeAll: () => { calls.order.push('preview-cleanup'); return previewCleanup.promise },
    },
    nativeWorkbenchSurfaces: { destroyAll: async () => {} },
    desktopBrowser: { close: async () => {} },
    destroyWindowsTray() {}, stopGateway() {},
    hasGatewayProcessExited: () => false,
    liveLifecycleOwnedGatewayProcesses: () => [child],
    drainOwnedGatewayForQuit: (process, url, requestShutdown) => {
      assert.equal(process, child)
      assert.equal(url, 'http://127.0.0.1:8765', 'shutdown retains the internal Gateway URL')
      assert.equal(requestShutdown, true)
      calls.order.push('gateway-shutdown')
      drainRequested.resolve()
      return drainResult.promise
    },
    dialog: { showErrorBox: (...args) => calls.errors.push(args) },
    desktopUpdateInstallMode: () => 'manual',
    createWindowsTray() {}, createApplicationMenu() {}, setDesktopUpdateState() {},
    setImmediate: callback => { calls.resumeQuit = callback },
  })
  runInContext([
    mainSection('let desktopOpenFlowRevision =', 'function beginDesktopWriterOperation('),
    mainSection('const gatewayState =', 'let sandboxUpgradeRefreshInFlight ='),
    mainSection('function publishGatewayConnection(', 'const artifactPreviewLeaseBroker ='),
    mainSection('function setAppExitPhase(', 'function destroyWindowsTray('),
    mainSection('function restoreDownloadedUpdateRetryState(', 'async function stopOwnedGatewaysForUpdate('),
    mainSection("app.on('before-quit',", 'function shutdownFromSignal('),
    "Object.assign(gatewayState, { status: 'ready', url: 'http://127.0.0.1:8765', port: 8765, owned: true })",
  ].join('\n'), context)
  return {
    context, calls, previewCleanup, drainRequested: drainRequested.promise, drainResult,
    snapshot: () => runInContext('desktopGatewayConnectionSnapshot()', context),
    phase: value => runInContext(`setAppExitPhase('${value}', 'synthetic lifecycle')`, context),
    quit: () => beforeQuit({ preventDefault() {} }),
  }
}

function assertRendererStopped(descriptor) {
  assert.equal(descriptor.status, 'stopped', 'shutdown must revoke renderer reconnect admission')
  assert.equal(descriptor.wsUrl, null)
  assert.equal(descriptor.authToken, null)
}

function runExitDescriptorCase() {
  const harness = mainExitHarness()
  const ready = harness.snapshot()
  assert.equal(ready.status, 'ready')
  assert.equal(ready.authToken, 'auth-synthetic-owner')
  harness.phase('deferred')
  assert.equal(harness.snapshot().status, 'ready', 'waiting for writers must not disconnect the renderer')
  assert.equal(harness.calls.published.length, 0)

  harness.phase('draining')
  assertRendererStopped(harness.snapshot())
  assertRendererStopped(harness.calls.published.at(-1))
  assert.equal(harness.calls.published.at(-1).revision, ready.revision + 1)
  assert.equal(runInContext('gatewayState.status', harness.context), 'ready', 'internal drain authority stays intact')
  assert.equal(harness.calls.refreshes, 0, 'drain publication must not start an optional HTTP diagnostic')
  harness.phase('committed')
  assertRendererStopped(harness.snapshot())
  assert.equal(harness.calls.published.length, 1, 'already stopped phase changes do not republish admission')
  runInContext("transitionGatewayConnection({ status: 'ready' })", harness.context)
  assertRendererStopped(harness.calls.published.at(-1))
  assert.equal(harness.calls.refreshes, 0, 'late ready notifications remain fenced during exit')

  harness.phase('running')
  const restored = harness.calls.published.at(-1)
  assert.equal(restored.status, 'ready', 'abandoned exit restores renderer admission')
  assert.equal(restored.wsUrl, ready.wsUrl)
  assert.equal(restored.authToken, ready.authToken)
  assert.equal(harness.calls.refreshes, 1)

  const directExit = mainExitHarness()
  directExit.phase('committed')
  assertRendererStopped(directExit.calls.published.at(-1))
}

async function runQuitDrainDescriptorCase(exited) {
  const harness = mainExitHarness()
  harness.quit()
  assertRendererStopped(harness.calls.published.at(-1))
  assert.deepEqual(harness.calls.order, ['descriptor:stopped', 'preview-cleanup'])
  harness.previewCleanup.resolve()
  await harness.drainRequested
  assert.deepEqual(harness.calls.order, ['descriptor:stopped', 'preview-cleanup', 'gateway-shutdown'])
  const drain = runInContext('quitGatewayDrainPromise', harness.context)
  harness.drainResult.resolve(exited)
  await drain
  if (exited) {
    assertRendererStopped(harness.snapshot())
    assert.deepEqual(harness.calls.exits, [0])
  } else {
    assert.equal(harness.calls.published.at(-1).status, 'ready', 'failed quit restores the live renderer')
    assert.equal(harness.calls.errors.length, 1)
    assert.deepEqual(harness.calls.exits, [])
  }
}

function runUpdateDrainRepeatedQuitCase() {
  const harness = mainExitHarness()
  runInContext('updateApplying = true', harness.context)
  harness.phase('deferred')
  harness.quit()
  assert.equal(harness.snapshot().status, 'ready')
  harness.phase('draining')
  harness.quit()
  assertRendererStopped(harness.snapshot())
  assert.equal(runInContext('appExitPhase', harness.context), 'draining', 'repeat Quit cannot reopen update drain admission')
  assert.equal(harness.calls.published.length, 1)
  runInContext('restoreDownloadedUpdateRetryState(null)', harness.context)
  assert.equal(harness.calls.published.at(-1).status, 'ready', 'failed update handoff restores admission')
  assert.equal(runInContext('appExitPhase', harness.context), 'running')
  harness.calls.resumeQuit()
  assert.equal(harness.calls.resumedQuits, 1, 'the deferred user quit is still resumed')
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
await runOptionalOnboardingDoesNotDelayReadyCase()
runExitDescriptorCase()
await runQuitDrainDescriptorCase(true)
await runQuitDrainDescriptorCase(false)
runUpdateDrainRepeatedQuitCase()
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
