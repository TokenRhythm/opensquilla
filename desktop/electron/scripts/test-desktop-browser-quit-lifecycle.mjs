import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { EventEmitter, once } from 'node:events'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'

import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'

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

function quitHarness(platform = process.platform) {
  const close = deferred()
  const drain = deferred()
  const app = new EventEmitter()
  const signals = new EventEmitter()
  signals.platform = platform
  const calls = { close: 0, drain: 0, quit: 0, exit: [], logs: [], terminalErrors: [] }
  let tabs = true
  const context = createContext({
    app, process: signals, isQuitting: false, appExitPhase: 'running',
    systemSessionEnding: false, updateApplying: false, updateInstallHandoffReady: false,
    quitRequestedDuringUpdateDrain: false, quitGatewayDrainPromise: null,
    quitDeferredForDesktopWriters: false, quitWriterAdmission: null,
    desktopWriters: new DesktopWriterAdmission(),
    desktopUpdateCheckScheduler: { stop() {} },
    desktopReliabilityTelemetry: { finishSession() {}, recordCrash() {} },
    nativeWorkbenchSurfaces: {
      hasBrowserTabs: () => tabs,
      closeBrowserTabs: () => { calls.close += 1; return close.promise },
      destroyAll: async () => {},
    },
    artifactPreviewLeaseBroker: { clear() {}, revokeAll: async () => {} },
    desktopBrowser: { close: async () => {} },
    destroyWindowsTray() {}, stopGateway() {},
    desktopLog: (event, detail) => calls.logs.push({ event, detail }),
    setAppExitPhase: phase => { context.appExitPhase = phase },
    desktopGatewayConnectionSuspendedForExit: () => ['draining', 'committed'].includes(context.appExitPhase),
    gatewayProcess: { pid: 1234 }, gatewayState: { status: 'ready', url: 'synthetic-gateway', owned: true },
    hasGatewayProcessExited: () => false,
    liveLifecycleOwnedGatewayProcesses: () => [context.gatewayProcess].filter(Boolean),
    drainOwnedGatewayForQuit: () => { calls.drain += 1; return drain.promise },
    waitForDesktopWriterOperations: async () => {},
    dialog: { showErrorBox() {} },
    gatewayUnexpectedExitRestartGeneration: 0,
    gatewayUnexpectedExitRestartTimer: null, gatewayUnexpectedExitRestartAttempt: 0,
    gatewayUnexpectedExitRestartProfileKey: null, gatewayUnexpectedExitRestartOpenFlowRevision: 0,
    desktopOpenFlowRevision: 7, desktopProfileKey: () => 'synthetic-profile',
    GATEWAY_UNEXPECTED_EXIT_RESTART_DELAYS_MS: [1_000, 2_000, 4_000],
    gatewayReadyProcesses: new WeakMap(), gatewayStoppingProcesses: new Set(),
    setTimeout, clearTimeout, sendBootStatus() {}, publishGatewayConnection() {},
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
    mainSection('let browserQuitClosePromise =', "app.on('window-all-closed',"),
  ].join('\n'), context)
  return {
    context, calls, close, drain,
    request: initiator => initiator === 'menu' ? app.quit() : signals.emit(initiator),
    finishClose: async accepted => {
      if (accepted) tabs = false
      close.resolve(accepted)
      await close.promise
      await Promise.resolve()
    },
    rejectClose: async () => {
      close.reject(new Error('Synthetic browser close failure'))
      await close.promise.catch(() => {})
      await Promise.resolve()
    },
    state: () => ({
      isQuitting: context.isQuitting, phase: context.appExitPhase,
      writerClosed: context.desktopWriters.closed,
      browserPending: runInContext('browserQuitClosePromise !== null', context),
    }),
    restartAllowed: () => {
      context.gatewayUnexpectedExitRestartProfileKey = 'synthetic-profile'
      context.gatewayUnexpectedExitRestartOpenFlowRevision = context.desktopOpenFlowRevision
      return runInContext('gatewayUnexpectedExitRestartAuthorityIsCurrent(gatewayUnexpectedExitRestartGeneration)', context)
    },
  }
}

async function settleClose(harness, outcome) {
  if (outcome === 'reject') await harness.rejectClose()
  else await harness.finishClose(false)
}

for (const platform of ['darwin', 'win32', 'linux']) {
  for (const initiator of ['menu', 'SIGINT', 'SIGTERM']) {
    for (const outcome of ['cancel', 'reject']) {
      const harness = quitHarness(platform)
      harness.request(initiator)
      harness.request(initiator)
      harness.request(initiator)
      harness.request(initiator)
      assert.equal(harness.calls.close, 1, 'repeated requests share one browser close decision')
      await settleClose(harness, outcome)
      assert.deepEqual(harness.state(), {
        isQuitting: false, phase: 'running', writerClosed: false, browserPending: false,
      }, `${platform} ${initiator} ${outcome}: abandoned quit preserves the running lifecycle`)
      assert.equal(harness.restartAllowed(), true, 'Gateway recovery remains admitted after the canceled quit')
      assert.equal(harness.calls.drain, 0, 'browser cancellation never starts a Gateway drain')
      assert.deepEqual(harness.calls.exit, [])
      harness.request(initiator)
      await Promise.resolve()
      assert.equal(harness.calls.close, 2, 'a later quit gets a fresh browser close attempt')
      assert.equal(harness.context.isQuitting, false)
    }

    const accepted = quitHarness(platform)
    accepted.request(initiator)
    await accepted.finishClose(true)
    assert.equal(accepted.context.isQuitting, true, 'accepted quit owns the shutdown state')
    assert.equal(accepted.context.appExitPhase, 'draining')
    assert.equal(accepted.context.desktopWriters.closed, true)
    assert.equal(accepted.calls.drain, 1)
    accepted.request('SIGINT')
    accepted.request('SIGTERM')
    accepted.request('menu')
    assert.equal(accepted.calls.close, 1)
    assert.equal(accepted.calls.drain, 1, 'repeated signals cannot start another Gateway drain')
    assert.equal(accepted.restartAllowed(), false)
    accepted.drain.resolve(true)
    await accepted.context.quitGatewayDrainPromise
    assert.deepEqual(accepted.calls.exit, [0], 'Electron exits only after the owned child drains')
    assert.equal(accepted.context.appExitPhase, 'committed')
  }

  for (const phase of ['draining', 'committed']) {
    for (const outcome of ['cancel', 'reject']) {
      const harness = quitHarness(platform)
      harness.request('SIGTERM')
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
