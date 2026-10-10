import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'

import {
  DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS,
  GatewayReadinessTimeoutError,
  lifecycleAllowsProcessSpawn,
  stopAndJoinLifecycleProcesses,
  waitForGatewayReadiness,
} from '../dist/gateway-lifecycle.js'
import { DesktopRoutingConfigurationError } from '../dist/desktop-router-profiles.js'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { WindowsUpdateCoordinator, WindowsUpdatePreparationError } from '../dist/windows-update-coordinator.js'
import { WindowsUpdateSecurityError } from '../dist/windows-update-security.js'
import { WindowsUpdateHandoffError } from '../dist/windows-update-handoff.js'
import { UpdateChannelError } from '../dist/update-channel.js'

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

function runCleanExitRecoveryContractCase() {
  const closeHandler = mainSection("child.once('close', (code, signal) => {", "  // A failed spawn")
  const stoppingMarker = mainSection(
    'function trackStoppingGatewayProcess',
    'function liveLifecycleOwnedGatewayProcesses',
  )
  assert.match(
    closeHandler,
    /const unexpectedReadyExit = [\s\S]*&& childWasReady[\s\S]*&& !isQuitting[\s\S]*&& !gatewayStoppingProcesses\.has\(child\)/,
    'a ready Gateway clean exit must be classified as unexpected while Desktop is alive',
  )
  assert.match(
    closeHandler,
    /if \(abnormalExit \|\| unexpectedReadyExit\) \{/,
    'clean ready exits must enter the bounded Gateway recovery series',
  )
  assert.match(
    stoppingMarker,
    /child\.once\('close',/,
    'intentional-stop markers must survive exit until the close classifier runs',
  )
  assert.doesNotMatch(
    stoppingMarker,
    /child\.once\('exit',/,
    'intentional-stop markers must not be cleared on exit before close',
  )
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
    GatewayReadinessTimeoutError,
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
    desktopStartupLog() {},
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
    nativeWorkbenchSurfaces: { hasBrowserTabs: () => false, destroyAll: async () => {} },
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

function lateReadyHarness() {
  let now = 0
  const sleepers = []
  const child = { pid: 101, exitCode: null, signalCode: null }
  const launch = { nonce: 'launch-101', port: 18791 }
  const calls = { published: [], ready: 0, probes: 0, ownership: 0, starts: 0, failures: [], rendererLoads: [] }
  const rendererUrl = 'opensquilla-app://desktop'
  const window = {
    documentUrl: rendererUrl,
    loadFile: async () => { window.documentUrl = 'file://synthetic-boot.html' },
    loadURL: async url => { calls.rendererLoads.push(url); window.documentUrl = url },
  }
  const handlers = new Map()
  const context = createContext({
    Error, Date, setTimeout, clearTimeout, performance: { now: () => now },
    DesktopRoutingConfigurationError,
    DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS, GatewayReadinessTimeoutError,
    isQuitting: false, updateApplying: false, appExitPhase: 'running',
    desktopWriters: { closed: false },
    gatewayProcess: child, gatewayProfileKey: 'profile-a',
    profileKey: 'profile-a',
    gatewayStoppingProcesses: new Set(),
    gatewayProcessOwnershipContexts: new Map([[child, launch]]),
    forceOnboardingOnNextStartup: false, onboardingPromptProfileKey: null,
    onboardingFlows: { active: null }, bootError: null,
    gatewayStartPromise: null, gatewayStartTelemetryAttempt: null,
    invalidateSecretStorageBackendCache() {},
    createGatewayStartTelemetryAttempt: () => ({}), finishGatewayStartTelemetry() {},
    DesktopStartupError: class extends Error {},
    bootPagePath: () => 'synthetic-boot.html',
    ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
    activeDesktopProfile: () => ({ home: context.profileKey }),
    desktopProfileFingerprint: () => 'profile-fingerprint',
    desktopProfileKey: () => context.profileKey,
    cancelGatewayUnexpectedExitRestart() {},
    createMainWindow: async () => {}, focusMainWindow() {},
    inspectActiveProfileBeforeStartup: async () => true,
    syncDesktopConsentMirror: async () => {}, desktopTelemetryRuntimeGate: { close() {} },
    desktopLog() {}, desktopStartupLog() {}, beginGatewayStartTelemetry() {},
    advanceGatewayStartTelemetry() {}, markGatewayProcessReady() {},
    sendBootStatus() {},
    DESKTOP_RENDERER_URL: rendererUrl,
    isCurrentWindowAtDesktopRenderer: candidate => candidate.documentUrl === rendererUrl,
    currentMainWindow: () => window,
    sendBootError: error => { context.bootError = error },
    finishAppStartFailure: error => calls.failures.push(error.message),
    finishAppStartSuccess: () => { calls.ready += 1 },
    publishGatewayConnection: () => {
      calls.published.push(runInContext('({ ...gatewayState })', context))
    },
    readinessCheck: async () => { calls.probes += 1; return now >= 126_000 },
    verifyOwnedGatewayLaunch: async () => { calls.ownership += 1; return true },
    waitForGatewayReadiness: async options => {
      if (now === 0) {
        now = 120_000
        return { status: 'timeout' }
      }
      return await waitForGatewayReadiness({
        ...options,
        now: () => now,
        sleep: milliseconds => new Promise(resolve => sleepers.push({ at: now + milliseconds, resolve })),
      })
    },
  })
  runInContext([
    mainSection('let desktopOpenFlowRevision =', 'function beginDesktopWriterOperation('),
    mainSection('const gatewayState =', 'let sandboxUpgradeRefreshInFlight ='),
    mainSection('async function waitForGateway(', 'function trackStoppingGatewayProcess('),
    mainSection('async function resumeOwnedGatewayStartup(', 'function publishResumedOwnedGateway('),
    mainSection('function currentBootResumeAuthority(', 'async function resumeBootStartup('),
    mainSection('async function resumeBootStartup(', "ipcMain.handle('desktop:boot:retry'"),
    mainSection('function publishResumedOwnedGateway(', '// Foreground startup still ends'),
    mainSection('function setAppExitPhase(', 'function destroyWindowsTray('),
    mainSection('async function loadDesktopRendererIntoCurrentWindow(', '// The old boot document'),
    mainSection('async function openOrResumeDesktopApp(', '// SIGKILL deadline'),
    // The baseline has no late observer. Keep the regression runnable against
    // that baseline so it fails on the missing ready publication, not parsing.
    main.includes('const GATEWAY_LATE_READY_OBSERVATION_MS =')
      ? mainSection('const GATEWAY_LATE_READY_OBSERVATION_MS =', 'const VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS =')
      : '',
    `gatewayState.url = 'http://127.0.0.1:18791'; gatewayState.port = 18791;
     gatewayState.owned = true; gatewayState.status = 'starting';
     gatewayConnectionInstanceId = 'instance-a';`,
  ].join('\n'), context)
  context.desktopGatewayConnectionSuspendedForExit = () => false
  context.rebuildWindowsTrayMenu = () => {}
  context.reuseHealthyGatewayState = async () => null
  context.ensureGatewayStarted = async () => {
    calls.starts += 1
    await runInContext('waitForGateway(gatewayState.url)', context)
    return runInContext('gatewayState', context)
  }
  const flush = async () => { for (let index = 0; index < 30; index += 1) await Promise.resolve() }
  return {
    context, calls, child, launch, window,
    open: () => runInContext('openOrResumeDesktopApp()', context),
    state: () => runInContext('({ ...gatewayState, bootError })', context),
    resume: () => handlers.get('desktop:boot:resume')(),
    advance: async milliseconds => {
      now += milliseconds
      const due = sleepers.splice(0).filter(item => {
        if (item.at <= now) { item.resolve(); return false }
        return true
      })
      sleepers.push(...due)
      await flush()
    },
    flush,
  }
}

async function runReadinessAfterForegroundTimeoutCase() {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  assert.equal(harness.state().status, 'error', 'the existing 120 second foreground deadline remains visible')
  assert.equal(harness.calls.failures.length, 1)
  await harness.advance(6_000)
  assert.equal(harness.state().status, 'ready', 'the same owned child becoming ready after 120 seconds must reconnect')
  assert.equal(harness.state().bootError, null, 'late readiness clears the startup error')
  assert.equal(harness.calls.starts, 1, 'late recovery cannot spawn a replacement')
  assert.equal(harness.calls.ownership, 1, 'readiness alone cannot grant ownership')
  assert.equal(harness.calls.published.filter(state => state.status === 'ready').length, 1)
  assert.equal(harness.calls.rendererLoads.length, 0, 'an existing renderer must not reload on late readiness')
}

async function runManualResumeTimeoutThenLateReadinessCase() {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  let ready = false
  harness.context.readinessCheck = async () => { harness.calls.probes += 1; return ready }
  const manual = harness.resume()
  await harness.flush()
  await harness.advance(120_000)
  assert.equal((await manual).ok, false, 'manual Resume retains its own foreground deadline')
  assert.equal(harness.state().status, 'error')
  assert.equal(harness.window.documentUrl, 'file://synthetic-boot.html')
  await harness.flush()
  ready = true
  await harness.advance(2_000)
  assert.equal(harness.state().status, 'ready')
  assert.equal(harness.window.documentUrl, 'opensquilla-app://desktop', 'late recovery after manual Resume must leave the boot page')
  assert.equal(harness.calls.rendererLoads.length, 1)
  assert.equal(harness.calls.published.filter(state => state.status === 'ready').length, 1)
  assert.equal(harness.state().bootError, null)
}

async function runLateReadinessNavigationAuthorityCase(label, invalidate) {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  const error = harness.state().bootError
  const navigation = deferred()
  harness.window.documentUrl = 'file://synthetic-boot.html'
  harness.window.loadURL = async url => {
    harness.calls.rendererLoads.push(url)
    await navigation.promise
    harness.window.documentUrl = url
  }
  await harness.advance(6_000)
  assert.equal(harness.calls.rendererLoads.length, 1, 'hold the real renderer loader at its navigation await')
  assert.equal(harness.calls.ready, 0, 'ready cannot be published before renderer restoration finishes')
  invalidate(harness)
  navigation.resolve()
  await harness.flush()
  assert.equal(harness.calls.ready, 0, `${label}: finishing navigation cannot restore superseded readiness`)
  assert.equal(harness.calls.published.filter(state => state.status === 'ready').length, 0)
  assert.equal(harness.state().bootError, error, 'a stale navigation cannot clear the current startup error')
}

async function runLateReadinessAuthorityCase(label, invalidate) {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  const verification = deferred()
  harness.context.verifyOwnedGatewayLaunch = async () => {
    harness.calls.ownership += 1
    return await verification.promise
  }
  await harness.advance(6_000)
  assert.equal(harness.calls.ownership, 1, 'hold the probe at the asynchronous ownership boundary')
  invalidate(harness)
  verification.resolve(true)
  await harness.flush()
  assert.equal(harness.calls.ready, 0, `${label}: a late callback cannot publish app readiness`)
  assert.equal(harness.calls.published.filter(state => state.status === 'ready').length, 0, label)
  assert.equal(harness.calls.starts, 1, `${label}: observation never spawns a successor`)
}

async function runLateReadinessDeadlineCase() {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  const error = harness.state().bootError
  await harness.advance(10 * 60_000)
  assert.equal(harness.state().status, 'error', 'the observation window is finite')
  assert.equal(harness.state().bootError, error, 'expiry preserves the original actionable error')
  assert.equal(runInContext('gatewayLateReadyObservation', harness.context), null)
  assert.equal(harness.calls.starts, 1)
  assert.equal((await harness.resume()).ok, true, 'manual Resume remains available after observation expires')
  assert.equal(harness.state().status, 'ready')
}

async function runForeignLateListenerCase() {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  harness.context.verifyOwnedGatewayLaunch = async () => {
    harness.calls.ownership += 1
    return false
  }
  await harness.advance(6_000)
  assert.equal(harness.calls.ownership, 1)
  assert.equal(harness.state().status, 'error', 'a healthy foreign listener is never adopted')
  assert.equal(harness.calls.ready, 0)
  assert.equal(harness.calls.starts, 1, 'the background observer has no port recovery or kill path')
  await harness.advance(10 * 60_000)
}

async function runLateReadinessManualResumeCase() {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  const backgroundVerification = deferred()
  harness.context.verifyOwnedGatewayLaunch = async () => {
    harness.calls.ownership += 1
    return harness.calls.ownership === 1 ? await backgroundVerification.promise : true
  }
  await harness.advance(6_000)
  const first = harness.resume()
  const repeated = harness.resume()
  assert.equal((await first).ok, true)
  assert.equal((await repeated).ok, true)
  backgroundVerification.resolve(true)
  await harness.flush()
  assert.equal(harness.calls.ownership, 2, 'repeated Resume shares one foreground attempt')
  assert.equal(harness.calls.ready, 1, 'manual Resume supersedes the in-flight background callback')
  assert.equal(harness.calls.published.filter(state => state.status === 'ready').length, 1)
  assert.equal(harness.state().bootError, null)
}

async function runLateReadinessSingleObserverCase() {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  runInContext('observeLateOwnedGatewayReadiness()', harness.context)
  await harness.flush()
  assert.equal(harness.calls.probes, 1, 'duplicate observation requests share the existing bounded attempt')
  await harness.advance(6_000)
  assert.equal(harness.calls.ready, 1)
}

function installFailedUpdateHarness(harness) {
  const verification = deferred()
  const calls = { stops: 0, launches: 0, quits: 0, verifies: 0 }
  Object.assign(harness.context, {
    WindowsUpdateCoordinator, WindowsUpdatePreparationError, WindowsUpdateSecurityError,
    WindowsUpdateHandoffError, UpdateChannelError, setImmediate,
    app: { getVersion: () => 'synthetic-old', quit: () => { calls.quits += 1 } },
    restoreWindowsUpdateCache: async () => {},
    desktopUpdateCandidate: { version: 'synthetic-new', tag: 'synthetic-new' },
    desktopUpdateStatus: 'downloaded', verifiedManualInstallerPath: 'synthetic-installer',
    windowsUpdateCoordinator: new WindowsUpdateCoordinator(), windowsUpdateRecoveryGeneration: 0,
    updateInstallHandoffReady: false, updateDownloadInProgress: false,
    manualInstallerActionInProgress: false, quitRequestedDuringUpdateDrain: false,
    downloadedUpdateVersion: null, desktopWriters: new DesktopWriterAdmission(),
    windowsInstallerActionsSupported: () => true, desktopUpdateInstallMode: () => 'manual',
    liveLifecycleOwnedGatewayProcesses: () => [harness.context.gatewayProcess].filter(Boolean),
    revalidateReadyWindowsInstaller: async () => { calls.verifies += 1; return await verification.promise },
    assertUnambiguousWindowsInstallation: async () => {},
    stopOwnedGatewaysForUpdate: async () => { calls.stops += 1; return true },
    launchWindowsInstaller: async () => { calls.launches += 1 },
    desktopReliabilityTelemetry: { clearUpdateHandoff() {}, recordUpdateResult() {} },
    classifyDesktopUpdateError: () => 'install_failed',
    classifyDesktopUpdateTelemetryError: () => 'install_failed',
    desktopUpdateErrorMessage: () => 'Synthetic installer verification failure',
    createWindowsTray() {}, createApplicationMenu() {}, setDesktopUpdateState() {},
  })
  runInContext([
    mainSection('function restoreDownloadedUpdateRetryState(', 'async function stopOwnedGatewaysForUpdate('),
    mainSection('async function applyWindowsInstaller(', '// Stop the owned gateway child'),
  ].join('\n'), harness.context)
  return {
    calls,
    start: () => runInContext('applyWindowsInstaller()', harness.context),
    fail: () => verification.reject(new Error('Synthetic installer verification failure')),
  }
}

async function runLateReadinessAfterFailedUpdateCase() {
  const harness = lateReadyHarness()
  await harness.open()
  await harness.flush()
  const oldVerification = deferred()
  const resumedVerification = deferred()
  harness.context.verifyOwnedGatewayLaunch = async () => {
    harness.calls.ownership += 1
    return await (harness.calls.ownership === 1 ? oldVerification.promise : resumedVerification.promise)
  }
  await harness.advance(6_000)
  assert.equal(harness.calls.ownership, 1, 'hold the original observer at its ownership check')
  const update = installFailedUpdateHarness(harness)
  const applying = update.start()
  await harness.flush()
  assert.equal(harness.context.appExitPhase, 'deferred')
  assert.equal(runInContext('gatewayLateReadyObservation', harness.context), null)
  update.fail()
  await applying
  await harness.flush()
  assert.equal(harness.calls.ownership, 2, 'failed verification starts a fresh ownership probe')
  const resumedObservation = runInContext('gatewayLateReadyObservation', harness.context)
  oldVerification.resolve(true)
  await harness.flush()
  assert.equal(harness.calls.ready, 0, 'the pre-update callback remains canceled after recovery')
  assert.equal(runInContext('gatewayLateReadyObservation', harness.context), resumedObservation,
    'the canceled callback cannot clear the resumed observation')
  resumedVerification.resolve(true)
  await harness.flush()
  assert.equal(harness.calls.ready, 1, 'failed verification must resume observation of the same live child')
  assert.equal(harness.state().bootError, null)
  assert.equal(harness.calls.published.filter(state => state.status === 'ready').length, 1,
    'the canceled pre-update callback cannot publish a second descriptor')
  assert.equal(harness.calls.starts, 1, 'update recovery must not spawn another Gateway')
  assert.equal(update.calls.stops, 0, 'verification failed before Gateway stop')
  assert.equal(update.calls.launches, 0, 'failed verification never reaches installer handoff')
}

async function runForegroundTimeoutDuringUpdateCase(label, invalidate = null, timeout = true) {
  const harness = lateReadyHarness()
  const foreground = deferred()
  const originalWait = harness.context.waitForGatewayReadiness
  let firstWait = true
  harness.context.waitForGatewayReadiness = options => {
    if (!firstWait) return originalWait(options)
    firstWait = false
    return foreground.promise
  }
  const opening = harness.open()
  await harness.flush()
  const update = installFailedUpdateHarness(harness)
  const applying = update.start()
  await harness.flush()
  assert.equal(harness.context.appExitPhase, 'deferred')
  await harness.advance(120_000)
  if (timeout) foreground.resolve({ status: 'timeout' })
  else foreground.reject(new Error('Synthetic unrelated startup failure'))
  await opening
  const originalError = harness.state().bootError
  assert.equal(harness.state().status, 'error')
  await harness.advance(6_000)
  assert.equal(harness.calls.probes, 0, `${label}: update verification cannot start a background readiness probe`)
  assert.equal(harness.calls.ready, 0, `${label}: update verification owns readiness publication`)
  await invalidate?.(harness)
  update.fail()
  await applying
  await harness.flush()
  await harness.advance(2_000)
  if (!invalidate && timeout) {
    assert.equal(harness.state().status, 'ready',
      'a foreground timeout during failed update verification must resume the same launch')
    assert.equal(harness.calls.ready, 1)
    assert.equal(harness.state().bootError, null)
    assert.equal(harness.calls.published.filter(state => state.status === 'ready').length, 1)
  } else {
    assert.equal(harness.calls.ready, 0, `${label}: canceled or unrelated startup must not recover`)
    assert.equal(harness.calls.probes, 0, `${label}: canceled or unrelated startup must not start a new probe`)
    assert.equal(harness.state().bootError, originalError)
  }
  assert.equal(harness.calls.starts, 1, `${label}: update failure cannot spawn another Gateway`)
  assert.equal(update.calls.stops, 0, `${label}: verification failed before Gateway stop`)
  assert.equal(update.calls.launches, 0, `${label}: verification failed before installer launch`)
}

async function runFailedUpdateKeepsLateReadinessDeadlineCase() {
  const harness = lateReadyHarness()
  harness.context.readinessCheck = async () => false
  await harness.open()
  const originalError = harness.state().bootError
  const first = installFailedUpdateHarness(harness)
  const firstApplying = first.start()
  await harness.flush()
  await harness.advance(590_000)
  first.fail()
  await firstApplying
  assert.ok(runInContext('gatewayLateReadyObservation', harness.context),
    'verification failure with time remaining resumes observation')
  const second = installFailedUpdateHarness(harness)
  const secondApplying = second.start()
  await harness.flush()
  await harness.advance(5_000)
  second.fail()
  await secondApplying
  assert.ok(runInContext('gatewayLateReadyObservation', harness.context))
  await harness.advance(5_001)
  assert.equal(runInContext('gatewayLateReadyObservation', harness.context), null,
    'repeated update failures cannot extend the original ten-minute deadline')
  harness.context.readinessCheck = async () => true
  await harness.advance(2_000)
  assert.equal(harness.calls.ready, 0)
  assert.equal(harness.state().bootError, originalError)
  assert.equal(harness.calls.starts, 1)
  assert.equal((await harness.resume()).ok, true, 'manual Resume remains available after the original deadline')
  assert.equal(harness.calls.ready, 1)
}

async function runFailedUpdateInvalidatesLateReadinessCase(label, invalidate) {
  const harness = lateReadyHarness()
  await harness.open()
  const update = installFailedUpdateHarness(harness)
  const applying = update.start()
  await harness.flush()
  await invalidate(harness)
  update.fail()
  await applying
  await harness.advance(6_000)
  assert.equal(harness.calls.ready, 0, `${label}: failed update cannot revive superseded startup`)
  assert.equal(runInContext('gatewayLateReadyObservation', harness.context), null, label)
  assert.equal(harness.calls.starts, 1, `${label}: no successor is spawned`)
  assert.equal(update.calls.stops, 0, `${label}: verification failed before stop`)
  assert.equal(update.calls.launches, 0, label)
  if (label === 'queued Quit') {
    await new Promise(setImmediate)
    assert.equal(update.calls.quits, 1, 'the queued Quit retains ownership of recovery')
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
runCleanExitRecoveryContractCase()
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
await runReadinessAfterForegroundTimeoutCase()
await runLateReadinessSingleObserverCase()
await runLateReadinessAfterFailedUpdateCase()
await runForegroundTimeoutDuringUpdateCase('same launch')
await runForegroundTimeoutDuringUpdateCase('unrelated startup error', null, false)
for (const [label, invalidate] of [
  ['replacement child', harness => { harness.context.gatewayProcess = { ...harness.child, pid: 202 } }],
  ['profile change', harness => { harness.context.profileKey = 'profile-b' }],
  ['new open revision', harness => runInContext('invalidateDesktopOpenFlow()', harness.context)],
  ['queued Quit', harness => { harness.context.quitRequestedDuringUpdateDrain = true }],
  ['another writer owner', harness => { harness.context.desktopWriters.close('synthetic cleanup') }],
  ['expired observation', harness => harness.advance(600_001)],
  ['stopping child', harness => harness.context.gatewayStoppingProcesses.add(harness.child)],
  ['child exit', harness => { harness.child.exitCode = 0 }],
  ['endpoint change', harness => runInContext("gatewayState.url = 'http://127.0.0.1:18792'", harness.context)],
  ['port change', harness => runInContext('gatewayState.port = 18792', harness.context)],
  ['instance change', harness => runInContext("gatewayConnectionInstanceId = 'instance-b'", harness.context)],
  ['launch context change', harness => harness.context.gatewayProcessOwnershipContexts.set(harness.child, { ...harness.launch })],
]) await runForegroundTimeoutDuringUpdateCase(label, invalidate)
await runFailedUpdateKeepsLateReadinessDeadlineCase()
for (const [label, invalidate] of [
  ['expired observation', harness => harness.advance(600_001)],
  ['replacement child', harness => { harness.context.gatewayProcess = { ...harness.child, pid: 202 } }],
  ['profile change', harness => { harness.context.profileKey = 'profile-b' }],
  ['new open revision', harness => runInContext('invalidateDesktopOpenFlow()', harness.context)],
  ['queued Quit', harness => { harness.context.quitRequestedDuringUpdateDrain = true }],
  ['another writer owner', harness => { harness.context.desktopWriters.close('synthetic cleanup') }],
  ['stopping child', harness => harness.context.gatewayStoppingProcesses.add(harness.child)],
  ['child exit', harness => { harness.child.exitCode = 0 }],
  ['endpoint change', harness => runInContext("gatewayState.url = 'http://127.0.0.1:18792'", harness.context)],
  ['port change', harness => runInContext('gatewayState.port = 18792', harness.context)],
  ['instance change', harness => runInContext("gatewayConnectionInstanceId = 'instance-b'", harness.context)],
  ['launch context change', harness => harness.context.gatewayProcessOwnershipContexts.set(harness.child, { ...harness.launch })],
]) await runFailedUpdateInvalidatesLateReadinessCase(label, invalidate)
for (const [label, invalidate] of [
  ['replacement child', harness => { harness.context.gatewayProcess = { ...harness.child, pid: 202 } }],
  ['profile change', harness => { harness.context.profileKey = 'profile-b' }],
  ['new open revision', harness => runInContext('invalidateDesktopOpenFlow()', harness.context)],
  ['quit', harness => { harness.context.isQuitting = true }],
  ['writer admission', harness => { harness.context.desktopWriters.closed = true }],
  ['update', harness => { harness.context.updateApplying = true }],
  ['update then failure', harness => runInContext("setAppExitPhase('deferred', 'update'); setAppExitPhase('running', 'update failed')", harness.context)],
  ['stopping child', harness => harness.context.gatewayStoppingProcesses.add(harness.child)],
  ['child exit', harness => { harness.child.exitCode = 0 }],
  ['endpoint change', harness => runInContext("gatewayState.url = 'http://127.0.0.1:18792'", harness.context)],
  ['port change', harness => runInContext('gatewayState.port = 18792', harness.context)],
  ['instance change', harness => runInContext("gatewayConnectionInstanceId = 'instance-b'", harness.context)],
  ['launch context change', harness => harness.context.gatewayProcessOwnershipContexts.set(harness.child, { ...harness.launch })],
]) await runLateReadinessAuthorityCase(label, invalidate)
await runForeignLateListenerCase()
await runLateReadinessDeadlineCase()
await runLateReadinessManualResumeCase()
await runManualResumeTimeoutThenLateReadinessCase()
for (const [label, invalidate] of [
  ['quit during renderer restoration', harness => { harness.context.isQuitting = true }],
  ['update during renderer restoration', harness => { harness.context.updateApplying = true }],
  ['new open during renderer restoration', harness => runInContext('invalidateDesktopOpenFlow()', harness.context)],
]) await runLateReadinessNavigationAuthorityCase(label, invalidate)

console.log('desktop gateway lifecycle tests passed')
