import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { EventEmitter, once } from 'node:events'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'

import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { DesktopQuitBudget, DesktopQuitConfirmation } from '../dist/desktop-quit.js'

const main = readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8')
function mainSection(start, end, after = '') {
  const scope = main.indexOf(after)
  assert.notEqual(scope, -1, after)
  const from = main.indexOf(start, scope)
  assert.notEqual(from, -1, start)
  const to = main.indexOf(end, from)
  assert.notEqual(to, -1, end)
  return main.slice(from, to)
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, decline) => { resolve = accept; reject = decline })
  return { promise, resolve, reject }
}

function quitHarness(platform = process.platform, { tabCount = 1, activeCount = 1, owned = true } = {}) {
  let close = deferred()
  const drain = deferred()
  const app = new EventEmitter()
  const signals = new EventEmitter()
  signals.platform = platform
  const calls = { close: 0, destroyedTabs: 0, drain: 0, legacyDrain: 0, cancelDrain: 0, activity: 0, confirm: 0, quit: 0, exit: [], logs: [], terminalErrors: [], order: [] }
  let tabs = tabCount
  let browserRevision = 0
  const stopChild = async legacy => {
    calls.drain += 1
    calls.order.push('gateway-drain')
    calls[legacy ? 'legacyDrain' : 'cancelDrain'] += 1
    const exited = await drain.promise
    if (exited) context.gatewayProcess = null
    return exited
  }
  const context = createContext({
    app, process: signals, isQuitting: false, appExitPhase: 'running',
    systemSessionEnding: false, updateApplying: false, updateInstallHandoffReady: false,
    windowsSessionEndPreviousPhase: null, windowsSessionEndResetTimer: null,
    quitRequestedDuringUpdateDrain: false, quitGatewayDrainPromise: null,
    quitDeferredForDesktopWriters: false, quitWriterAdmission: null,
    quitFromSignal: false, quitConfirmed: false, quitConfirmationPromise: null,
    quitConfirmationWantsExit: false,
    desktopQuitConfirmation: new DesktopQuitConfirmation(), DesktopQuitBudget,
    gatewayProcessCleanupContexts: new Map(), gatewayProcessCleanupOperations: new WeakMap(),
    gatewayProcessTreeTerminations: new Map(),
    quitUnconfirmedProcessTrees: new Set(),
    desktopWriters: new DesktopWriterAdmission(),
    desktopUpdateCheckScheduler: { stop() {} },
    desktopReliabilityTelemetry: { finishSession() {}, recordCrash() {} },
    nativeWorkbenchSurfaces: {
      hasBrowserTabs: () => tabs > 0,
      closeBrowserTabs: async options => {
        calls.close += 1
        assert.equal(typeof options?.confirm, 'function', 'app quit supplies its unified confirmation')
        const snapshotTabs = tabs
        const snapshotRevision = browserRevision
        const unchanged = () => tabs === snapshotTabs && browserRevision === snapshotRevision
        const approved = await options.confirm({ tabCount: snapshotTabs })
        if (!approved) return false
        if (!unchanged()) return false
        if (snapshotTabs === 0) return true
        const accepted = await close.promise
        if (accepted && unchanged()) {
          calls.order.push('tabs-destroyed')
          calls.destroyedTabs += snapshotTabs
          tabs = 0
          browserRevision += 1
          return true
        }
        return false
      },
      destroyAll: async () => {},
    },
    artifactPreviewLeaseBroker: { clear() {}, revokeAll: async () => {}, shutdown: async () => {} },
    desktopBrowser: { close: async () => {} },
    destroyWindowsTray() {}, stopGateway() {},
    desktopLog: (event, detail) => calls.logs.push({ event, detail }),
    setAppExitPhase: phase => { context.appExitPhase = phase },
    desktopGatewayConnectionSuspendedForExit: () => ['draining', 'committed'].includes(context.appExitPhase),
    gatewayProcess: owned ? { pid: 1234 } : null,
    gatewayState: { status: 'ready', url: 'synthetic-gateway', owned },
    hasGatewayProcessExited: () => false,
    liveLifecycleOwnedGatewayProcesses: () => [context.gatewayProcess].filter(Boolean),
    quitOwnedGateway: () => stopChild(false),
    drainOwnedGatewayForLegacyQuit: () => stopChild(true),
    showDesktopQuitFailure: () => assert.fail('Synthetic Gateway must exit cleanly'),
    desktopAccounts: new Map(),
    ownedGatewayRecord: () => ({ pid: 1234 }),
    readVerifiedDesktopGatewayActivity: async () => {
      calls.activity += 1
      return { kind: 'ok', activeCount }
    },
    currentMainWindow: () => null, currentOnboardingWindow: () => null,
    showDesktopQuitConfirmation: async () => {
      calls.confirm += 1
      calls.order.push('confirmation')
      return { response: 'confirm', remember: false }
    },
    desktopT: key => key,
    desktopTv: (key, value) => `${key}:${value}`,
    waitForDesktopWriterOperations: () => context.desktopWriters.waitForAtMost(0),
    dialog: { showErrorBox() {} },
    gatewayUnexpectedExitRestartGeneration: 0,
    gatewayUnexpectedExitRestartTimer: null, gatewayUnexpectedExitRestartAttempt: 0,
    gatewayUnexpectedExitRestartProfileKey: null, gatewayUnexpectedExitRestartOpenFlowRevision: 0,
    desktopOpenFlowRevision: 7, desktopProfileKey: () => 'synthetic-profile',
    GATEWAY_UNEXPECTED_EXIT_RESTART_DELAYS_MS: [1_000, 2_000, 4_000],
    gatewayReadyProcesses: new WeakMap(), gatewayStoppingProcesses: new Set(),
    setTimeout, clearTimeout, setImmediate, sendBootStatus() {}, publishGatewayConnection() {},
    sendBootError: message => calls.terminalErrors.push(message),
    ensureGatewayStarted: async () => { throw new Error('Unexpected restart') },
  })
  app.quit = () => {
    calls.quit += 1
    let prevented = false
    app.emit('before-quit', { preventDefault: () => { prevented = true } })
    if (!prevented) calls.exit.push('ordinary')
  }
  app.exit = code => calls.exit.push(code)
  runInContext([
    mainSection('function invalidateGatewayUnexpectedExitRestart(', 'function ensureGatewayStarted('),
    mainSection('function holdQuitForTaskConfirmation(', 'async function confirmOwnedGatewayProcessCleanup('),
    mainSection("app.on('before-quit',", "app.on('window-all-closed',"),
  ].join('\n'), context)
  return {
    context, calls, drain,
    tabCount: () => tabs,
    setTabs: count => { tabs = count; browserRevision += 1 },
    request: initiator => initiator === 'menu' ? app.quit() : signals.emit(initiator),
    finishClose: async accepted => {
      close.resolve(accepted)
      await close.promise
      await context.quitConfirmationPromise
      close = deferred()
    },
    rejectClose: async () => {
      close.reject(new Error('Synthetic browser close failure'))
      await close.promise.catch(() => {})
      await context.quitConfirmationPromise
      close = deferred()
    },
    state: () => ({
      isQuitting: context.isQuitting, phase: context.appExitPhase,
      writerClosed: context.desktopWriters.closed,
      browserPending: context.quitConfirmationPromise !== null,
    }),
    restartAllowed: () => {
      context.gatewayUnexpectedExitRestartProfileKey = 'synthetic-profile'
      context.gatewayUnexpectedExitRestartOpenFlowRevision = context.desktopOpenFlowRevision
      return runInContext('gatewayUnexpectedExitRestartAuthorityIsCurrent(gatewayUnexpectedExitRestartGeneration)', context)
    },
  }
}

async function settleClose(harness, outcome) {
  await new Promise(resolve => setImmediate(resolve))
  if (outcome === 'reject') await harness.rejectClose()
  else await harness.finishClose(false)
}

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 100 && !predicate(); attempt += 1) {
    await new Promise(resolve => setImmediate(resolve))
  }
  assert.ok(predicate(), message)
}

for (const platform of ['darwin', 'win32', 'linux']) {
  for (const initiator of ['menu', 'SIGINT', 'SIGTERM']) {
    for (const outcome of ['cancel', 'reject']) {
      const harness = quitHarness(platform)
      harness.request(initiator)
      harness.request(initiator)
      harness.request(initiator)
      harness.request(initiator)
      await waitFor(() => harness.calls.confirm === 1, 'repeated quits reach one unified decision')
      assert.equal(harness.calls.close, 1, 'repeated requests share one browser close attempt')
      assert.equal(harness.tabCount(), 1, 'confirmation precedes destructive browser close')
      await settleClose(harness, outcome)
      assert.deepEqual(harness.state(), {
        isQuitting: false, phase: 'running', writerClosed: false, browserPending: false,
      }, `${platform} ${initiator} ${outcome}: abandoned quit preserves the running lifecycle`)
      assert.equal(harness.restartAllowed(), true, 'Gateway recovery remains admitted after the canceled quit')
      assert.equal(harness.calls.drain, 0, 'browser cancellation never starts a Gateway drain')
      assert.equal(harness.calls.destroyedTabs, 0, 'a canceled or changed snapshot preserves tabs')
      assert.equal(harness.context.quitFromSignal, false, 'canceled signals must not change a later menu quit')
      assert.deepEqual(harness.calls.exit, [])
      harness.request(initiator)
      await waitFor(() => harness.calls.confirm === 2, 'a later quit obtains a fresh decision')
      assert.equal(harness.calls.close, 2, 'a later quit gets a fresh browser close attempt')
      assert.equal(harness.context.isQuitting, false)
      await harness.finishClose(false)
    }

    const accepted = quitHarness(platform)
    accepted.request(initiator)
    await waitFor(() => accepted.calls.confirm === 1, 'pages and tasks share one confirmation')
    assert.equal(accepted.tabCount(), 1)
    assert.equal(accepted.calls.drain, 0)
    await accepted.finishClose(true)
    assert.equal(accepted.context.isQuitting, true, 'accepted quit owns the shutdown state')
    assert.equal(accepted.context.appExitPhase, 'draining')
    assert.equal(accepted.context.desktopWriters.closed, true)
    assert.equal(accepted.calls.drain, 1)
    assert.equal(accepted.calls.legacyDrain, initiator === 'menu' ? 0 : 1)
    assert.equal(accepted.calls.cancelDrain, initiator === 'menu' ? 1 : 0)
    assert.equal(accepted.calls.activity, initiator === 'menu' ? 1 : 0, 'signal quit retains its legacy task policy')
    assert.equal(accepted.calls.confirm, 1, 'page close never adds a second prompt after task confirmation')
    assert.deepEqual(accepted.calls.order, ['confirmation', 'tabs-destroyed', 'gateway-drain'])
    accepted.request('SIGINT')
    accepted.request('SIGTERM')
    accepted.request('menu')
    assert.equal(accepted.calls.close, 1)
    assert.equal(accepted.calls.drain, 1, 'repeated signals cannot start another Gateway drain')
    assert.equal(accepted.context.quitFromSignal, initiator !== 'menu', 'in-flight shutdown keeps its original policy')
    assert.equal(accepted.restartAllowed(), false)
    accepted.drain.resolve(true)
    await accepted.context.quitGatewayDrainPromise
    assert.deepEqual(accepted.calls.exit, [0], 'Electron exits only after the owned child drains')
    assert.equal(accepted.context.appExitPhase, 'committed')
  }

  for (const outcome of ['cancel', 'reject']) {
    for (const earlierRequest of ['SIGINT', 'SIGTERM', 'direct-quit-confirmation']) {
      const harness = quitHarness(platform)
      if (earlierRequest === 'direct-quit-confirmation') {
        runInContext('holdQuitForTaskConfirmation({ preventDefault() {} })', harness.context)
      } else harness.request(earlierRequest)
      await waitFor(() => harness.calls.confirm === 1, 'direct and signal quits use the same pending decision')
      assert.equal(harness.context.quitConfirmed, false, 'quit is uncommitted until the browser snapshot closes')
      await settleClose(harness, outcome)
      assert.equal(harness.context.quitConfirmed, false, 'browser cancellation discards only the completed quit confirmation')
      assert.equal(harness.context.quitFromSignal, false)
      const previousConfirmations = harness.calls.confirm
      harness.request('menu')
      await waitFor(() => harness.calls.confirm === previousConfirmations + 1, 'the next menu quit rechecks task activity')
      await harness.finishClose(true)
      assert.equal(harness.calls.confirm, previousConfirmations + 1, 'a later menu quit must confirm the still-active task')
      assert.equal(harness.calls.cancelDrain, 1)
      assert.equal(harness.calls.legacyDrain, 0, 'a canceled signal cannot impose the legacy drain on a user quit')
      harness.drain.resolve(true)
      await harness.context.quitGatewayDrainPromise
      assert.deepEqual(harness.calls.exit, [0])
    }
  }

  {
    const harness = quitHarness(platform)
    const taskDecision = deferred()
    harness.context.showDesktopQuitConfirmation = () => {
      harness.calls.confirm += 1
      return taskDecision.promise
    }
    harness.request('menu')
    const taskConfirmation = harness.context.quitConfirmationPromise
    await waitFor(() => harness.calls.confirm === 1, 'active-task confirmation is pending')
    harness.request('menu')
    harness.request('SIGTERM')
    assert.equal(harness.context.quitConfirmationPromise, taskConfirmation, 'repeated quits retain the same confirmation')
    assert.equal(harness.calls.close, 1)
    assert.equal(harness.calls.destroyedTabs, 0, 'task confirmation cannot destroy tabs before its answer')
    assert.equal(harness.tabCount(), 1)
    taskDecision.resolve({ response: 'cancel', remember: false })
    await taskConfirmation
    assert.equal(harness.tabCount(), 1, 'canceling active-task quit preserves the open page')
    assert.equal(harness.calls.destroyedTabs, 0)
    assert.equal(harness.calls.drain, 0)
    assert.equal(harness.context.quitConfirmed, false)
    assert.equal(harness.context.quitFromSignal, false)
    assert.deepEqual(harness.calls.exit, [])
    assert.equal(harness.restartAllowed(), true)
  }

  for (const options of [{ activeCount: 0 }, { owned: false }]) {
    const harness = quitHarness(platform, options)
    harness.request('menu')
    await waitFor(() => harness.calls.confirm === 1, 'open pages require confirmation even without active owned tasks')
    assert.equal(harness.tabCount(), 1)
    await harness.finishClose(true)
    assert.equal(harness.calls.confirm, 1)
    assert.equal(harness.calls.destroyedTabs, 1)
    assert.equal(harness.calls.activity, options.owned === false ? 0 : 1)
    if (options.owned !== false) {
      assert.equal(harness.calls.cancelDrain, 1)
      harness.drain.resolve(true)
    } else assert.equal(harness.calls.drain, 0, 'page-only quit does not drain an external Gateway')
    await harness.context.quitGatewayDrainPromise
    assert.deepEqual(harness.calls.exit, [0])
  }

  for (const mutation of ['new-page', 'navigation']) {
    const harness = quitHarness(platform)
    const finishWriter = harness.context.desktopWriters.begin('synthetic profile write')
    harness.request('menu')
    const confirmation = harness.context.quitConfirmationPromise
    await waitFor(() => harness.context.appExitPhase === 'deferred', 'accepted quit waits for the admitted writer')
    assert.equal(harness.calls.confirm, 1)
    assert.equal(harness.calls.destroyedTabs, 0, 'protected writer finishes before any page closes')
    assert.equal(harness.context.quitConfirmed, false)
    assert.equal(harness.context.desktopWriters.closed, true)
    harness.setTabs(mutation === 'new-page' ? 2 : 1)
    finishWriter()
    await confirmation
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(harness.state(), {
      isQuitting: false, phase: 'running', writerClosed: false, browserPending: false,
    }, `${mutation}: a changed snapshot cancels the quit after its writer settles`)
    assert.equal(harness.tabCount(), mutation === 'new-page' ? 2 : 1)
    assert.equal(harness.calls.destroyedTabs, 0)
    assert.equal(harness.calls.confirm, 1, 'snapshot rejection does not add another dialog')
    assert.equal(harness.calls.drain, 0)
    assert.equal(harness.calls.quit, 1, 'writer completion cannot resume an abandoned quit')
    assert.equal(harness.context.quitDeferredForDesktopWriters, false)
    assert.equal(harness.context.quitWriterAdmission, null)
    assert.equal(harness.context.quitFromSignal, false)
    assert.equal(harness.context.quitConfirmed, false)
    assert.deepEqual(harness.calls.exit, [])
  }

  {
    const harness = quitHarness(platform, { tabCount: 0 })
    const finishWriter = harness.context.desktopWriters.begin('synthetic signal-time write')
    harness.request('SIGTERM')
    const confirmation = harness.context.quitConfirmationPromise
    await waitFor(() => harness.context.appExitPhase === 'deferred', 'signal quit with a writer uses the same coordinator')
    assert.equal(harness.calls.confirm, 0, 'there were no pages requiring consent at the initial snapshot')
    assert.equal(harness.calls.close, 1)
    assert.equal(harness.context.quitFromSignal, true)
    harness.setTabs(1)
    finishWriter()
    await confirmation
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(harness.state(), {
      isQuitting: false, phase: 'running', writerClosed: false, browserPending: false,
    }, 'a page opened during the signal writer wait cannot inherit the empty snapshot')
    assert.equal(harness.tabCount(), 1)
    assert.equal(harness.calls.destroyedTabs, 0)
    assert.equal(harness.calls.drain, 0)
    assert.equal(harness.calls.quit, 1, 'no legacy writer continuation requests another quit')
    assert.equal(harness.context.quitDeferredForDesktopWriters, false)
    assert.equal(harness.context.quitFromSignal, false)
    assert.equal(harness.context.quitConfirmed, false)
    assert.equal(harness.context.quitWriterAdmission, null)
    assert.deepEqual(harness.calls.exit, [])
  }

  for (const owner of ['update', 'system']) {
    const harness = quitHarness(platform)
    const finishWriter = harness.context.desktopWriters.begin('synthetic writer before lifecycle takeover')
    harness.request('menu')
    const confirmation = harness.context.quitConfirmationPromise
    await waitFor(() => harness.context.appExitPhase === 'deferred', 'quit has accepted and is waiting for its writer')
    const quitAdmission = harness.context.quitWriterAdmission
    const authoritativeAdmission = harness.context.desktopWriters.close(`synthetic ${owner}`)
    harness.context.isQuitting = true
    if (owner === 'system') harness.context.systemSessionEnding = true
    else {
      harness.context.updateApplying = true
      harness.context.updateInstallHandoffReady = true
    }
    harness.request('menu')
    assert.equal(harness.context.appExitPhase, 'committed')
    finishWriter()
    await confirmation
    assert.deepEqual(harness.state(), {
      isQuitting: true, phase: 'committed', writerClosed: true, browserPending: false,
    }, `${owner}: an older writer wait cannot roll back the authoritative exit`)
    assert.equal(harness.context.desktopWriters.hasOwner(quitAdmission), false, 'abandoned quit reopens only its own token')
    assert.equal(harness.context.desktopWriters.hasOwner(authoritativeAdmission), true)
    assert.equal(harness.context.quitWriterAdmission, null)
    assert.equal(harness.calls.destroyedTabs, 0)
    assert.equal(harness.calls.drain, 0)
    assert.equal(harness.calls.confirm, 1)
  }

  for (const owner of ['update', 'system']) {
    const harness = quitHarness(platform)
    const closeBrowserTabs = harness.context.nativeWorkbenchSurfaces.closeBrowserTabs
    let quitAdmission
    let authoritativeAdmission
    harness.context.nativeWorkbenchSurfaces.closeBrowserTabs = async options => {
      const accepted = await closeBrowserTabs(options)
      assert.equal(accepted, true, 'native page close succeeds before lifecycle takeover')
      quitAdmission = harness.context.quitWriterAdmission
      authoritativeAdmission = harness.context.desktopWriters.close(`synthetic late ${owner}`)
      harness.context.isQuitting = true
      harness.context.appExitPhase = 'committed'
      if (owner === 'system') harness.context.systemSessionEnding = true
      else harness.context.updateApplying = true
      return accepted
    }
    harness.request('menu')
    await waitFor(() => harness.calls.confirm === 1, 'the native close decision is accepted')
    await harness.finishClose(true)
    assert.equal(harness.calls.destroyedTabs, 1)
    assert.ok(quitAdmission, 'this quit acquired its own writer token')
    assert.equal(harness.context.desktopWriters.hasOwner(quitAdmission), false,
      'successful native close does not retain a superseded quit token')
    assert.equal(harness.context.desktopWriters.hasOwner(authoritativeAdmission), true)
    assert.equal(harness.context.quitWriterAdmission, null)
    assert.deepEqual(harness.state(), {
      isQuitting: true, phase: 'committed', writerClosed: true, browserPending: false,
    }, `${owner}: takeover after native success retains its own phase and admission`)
    assert.equal(harness.calls.drain, 0)
    assert.equal(harness.calls.quit, 1, 'superseded native success cannot request another quit')
    assert.equal(harness.context.quitConfirmed, false)
  }

  if (platform === 'win32') {
    const harness = quitHarness(platform)
    const finishWriter = harness.context.desktopWriters.begin('synthetic write before Windows query')
    harness.request('menu')
    const confirmation = harness.context.quitConfirmationPromise
    await waitFor(() => harness.context.appExitPhase === 'deferred', 'quit waits for the writer before Windows queries session end')
    const quitAdmission = harness.context.quitWriterAdmission
    let resetSessionEnd
    harness.context.window = new EventEmitter()
    runInContext(mainSection('const commitSystemSessionEnd =', "window.on('session-end',"), harness.context)
    harness.context.setTimeout = (callback, timeoutMs) => {
      assert.equal(timeoutMs, 15_000)
      resetSessionEnd = callback
      return { unref() {} }
    }
    harness.context.window.emit('query-session-end')
    harness.context.setTimeout = setTimeout
    assert.equal(harness.context.systemSessionEnding, true)
    assert.equal(harness.context.isQuitting, false, 'query alone does not commit a real session end')
    assert.equal(harness.context.windowsSessionEndPreviousPhase, 'deferred')
    assert.equal(harness.context.appExitPhase, 'committed')
    finishWriter()
    await confirmation
    assert.equal(harness.context.desktopWriters.hasOwner(quitAdmission), false)
    assert.equal(harness.context.quitWriterAdmission, null)
    assert.equal(harness.context.desktopWriters.closed, false)
    assert.equal(harness.context.windowsSessionEndPreviousPhase, 'running',
      'an abandoned quit cannot remain the phase restored after Windows cancels shutdown')
    assert.equal(harness.context.appExitPhase, 'committed', 'pending Windows query keeps its current authority')
    assert.equal(harness.calls.destroyedTabs, 0)
    assert.equal(harness.tabCount(), 1)
    assert.equal(harness.calls.drain, 0)
    assert.equal(harness.calls.quit, 1)
    assert.equal(typeof resetSessionEnd, 'function')
    resetSessionEnd()
    assert.equal(harness.context.systemSessionEnding, false)
    assert.equal(harness.context.windowsSessionEndPreviousPhase, null)
    assert.deepEqual(harness.state(), {
      isQuitting: false, phase: 'running', writerClosed: false, browserPending: false,
    }, 'the production reset timer restores running with writer admission open')
  }

  for (const phase of ['draining', 'committed']) {
    for (const outcome of ['cancel', 'reject']) {
      const harness = quitHarness(platform)
      harness.request('SIGTERM')
      await waitFor(() => harness.calls.confirm === 1, 'browser decision is pending')
      // Another lifecycle operation becomes authoritative while browser close awaits.
      harness.context.isQuitting = true
      harness.context.appExitPhase = phase
      harness.context.desktopWriters.close('synthetic-lifecycle')
      await settleClose(harness, outcome)
      assert.deepEqual(harness.state(), {
        isQuitting: true, phase, writerClosed: true, browserPending: false,
      }, 'late browser cancellation cannot restore another operation to running')
      assert.equal(harness.restartAllowed(), false)
    }
  }

  for (const owner of ['update', 'system']) {
    for (const outcome of ['cancel', 'reject']) {
      const harness = quitHarness(platform)
      harness.request('SIGINT')
      await waitFor(() => harness.calls.confirm === 1, 'browser decision is pending before lifecycle takeover')
      harness.context.isQuitting = true
      harness.context.desktopWriters.close('synthetic-lifecycle')
      if (owner === 'system') harness.context.systemSessionEnding = true
      else {
        harness.context.updateApplying = true
        harness.context.updateInstallHandoffReady = true
      }
      harness.request('SIGTERM')
      assert.equal(harness.context.appExitPhase, 'committed')
      await settleClose(harness, outcome)
      assert.deepEqual(harness.state(), {
        isQuitting: true, phase: 'committed', writerClosed: true, browserPending: false,
      }, `${owner}: committed exit remains owned after an older browser decision settles`)
      assert.equal(harness.restartAllowed(), false)
    }
  }
}

async function syntheticChild(children) {
  const child = spawn(process.execPath, ['-e', `
    process.on('message', message => { if (message === 'exit') process.exit(13) })
    process.send('ready')
  `], { stdio: ['pipe', 'pipe', 'pipe', 'ipc'] })
  children.push(child)
  const [message] = await once(child, 'message', { signal: AbortSignal.timeout(5_000) })
  assert.equal(message, 'ready')
  return child
}

async function runChildRecoveryAfterAbandonedQuit(initiator, outcome) {
  const children = []
  const harness = quitHarness()
  const recovered = deferred()
  try {
    const child = await syntheticChild(children)
    const context = harness.context
    context.child = child
    context.gatewayProcess = child
    context.gatewayReadyProcesses.set(child, {
      profileKey: 'synthetic-profile', openFlowRevision: context.desktopOpenFlowRevision,
    })
    Object.assign(context, {
      gatewayOutputTail: '', childExitMessage: null,
      gatewayExitLooksLikePortInUse: () => false,
      classifyGatewayExitMessage: message => message,
      writeLogLine() {}, closeLogStream() {}, advanceGatewayStartTelemetry() {},
      hasExplicitGatewayPort: () => true,
      ensureGatewayStarted: async () => {
        const replacement = await syntheticChild(children)
        context.gatewayProcess = replacement
        context.gatewayState.status = 'ready'
        context.replacement = replacement
        runInContext('markGatewayProcessReady(replacement)', context)
        recovered.resolve(replacement)
        return context.gatewayState
      },
    })
    runInContext(mainSection(
      "child.once('close', (code, signal) => {", '// A failed spawn', 'async function startGateway()',
    ), context)
    harness.request(initiator)
    await settleClose(harness, outcome)
    child.send('exit')
    // Electron keeps its event loop alive while the production retry timer is unref'd.
    // A referenced fixture deadline gives this Node harness the same lifetime.
    let deadline
    try {
      await Promise.race([
        recovered.promise,
        new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('Synthetic child recovery timed out')), 5_000) }),
      ])
    } finally {
      clearTimeout(deadline)
    }
    assert.notEqual(context.gatewayProcess, child, 'an actual unexpected child exit starts a replacement')
    assert.equal(context.gatewayProcess.exitCode, null)
    assert.equal(context.gatewayState.status, 'ready')
    assert.equal(harness.calls.logs.filter(entry => entry.event === 'gateway_unexpected_exit_restart_attempt').length, 1)
    assert.deepEqual(harness.calls.terminalErrors, [])
  } finally {
    runInContext('invalidateGatewayUnexpectedExitRestart()', harness.context)
    for (const child of children) {
      if (!child.pid || child.exitCode !== null || child.signalCode !== null) continue
      const closed = once(child, 'close', { signal: AbortSignal.timeout(5_000) })
      if (child.connected) child.send('exit', error => { if (error) child.kill('SIGKILL') })
      else child.kill('SIGKILL')
      await closed
    }
  }
}

for (const initiator of ['menu', 'SIGINT', 'SIGTERM']) {
  for (const outcome of ['cancel', 'reject']) await runChildRecoveryAfterAbandonedQuit(initiator, outcome)
}

console.log('desktop browser quit lifecycle tests passed')
