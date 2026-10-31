import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { EventEmitter } from 'node:events'
import { join } from 'node:path'
import { mock } from 'node:test'
import { createContext, runInContext } from 'node:vm'
import ts from '@typescript/typescript6'

import { DesktopQuitBudget, DesktopQuitConfirmation, quitGatewayWithinBudget } from '../dist/desktop-quit.js'
import { buildDesktopQuitDialogHtml, normalizeDesktopQuitDialogPalette, parseDesktopQuitDialogResponse } from '../dist/desktop-quit-dialog.js'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { mainWindowCloseAction } from '../dist/desktop-window-lifecycle.js'
import { normalizeGatewayLocale } from '../dist/desktop-locale.js'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'

// Execute production handlers without Electron or real process termination.
const source = ts.createSourceFile(
  'main.js', readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
)
const functions = []
const productionQuitNames = new Set([
  'quitOwnedGateway', 'terminateOwnedGatewayForQuit',
  'confirmOwnedGatewayProcessCleanup', 'drainOwnedGatewayForLegacyQuit',
])
const productionQuitFunctions = []
let beforeQuitHandler
function collect(node) {
  if (ts.isFunctionDeclaration(node)
    && ['showDesktopQuitFailure', 'forceOwnedGatewayForQuit', 'terminateGatewayProcess'].includes(node.name?.text)) {
    functions.push(node.getText(source))
  }
  if (ts.isFunctionDeclaration(node) && productionQuitNames.has(node.name?.text)) {
    productionQuitFunctions.push(node.getText(source))
  }
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'app.on'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'before-quit') {
    beforeQuitHandler = node.arguments[1].getText(source)
  }
  ts.forEachChild(node, collect)
}
collect(source)
assert.equal(functions.length, 3)
assert.equal(productionQuitFunctions.length, productionQuitNames.size)
assert.ok(beforeQuitHandler)
const flush = () => new Promise(resolve => setImmediate(resolve))

async function scenario(run, { platform = 'darwin', exited = false, dev = false, productionQuit = false } = {}) {
  let now = 0
  let budgets = 0
  const calls = { exits: [], signals: [], errors: [], logs: [], drains: 0, prevented: 0 }
  const child = { pid: 12345, exitCode: exited ? 0 : null, signalCode: null,
    kill: signal => { calls.signals.push(['child', signal]); return true } }
  const launch = { instanceNonce: 'synthetic-retained-launch' }
  const env = {
    DesktopQuitBudget: class extends DesktopQuitBudget {
      constructor() { super(10_000, 5_000, () => now); budgets += 1 }
    },
    systemSessionEnding: false, updateApplying: false, updateInstallHandoffReady: false,
    quitRequestedDuringUpdateDrain: false, quitGatewayDrainPromise: null,
    holdQuitForTaskConfirmation: () => false, desktopUpdateCheckScheduler: { stop() {} },
    desktopAccounts: new Map(),
    desktopWriters: { activeCount: 0, close: () => Symbol('quit') },
    quitDeferredForDesktopWriters: false, quitWriterAdmission: null,
    desktopLog: (event, detail) => calls.logs.push({ event, ...detail }),
    process: { platform, kill: (pid, signal) => calls.signals.push(['group', pid, signal]) },
    isQuitting: false, appExitPhase: 'running',
    cancelGatewayUnexpectedExitRestart() {}, gatewayProcess: child,
    gatewayState: { owned: true, status: 'ready' },
    hasGatewayProcessExited: value => value.exitCode !== null || value.signalCode !== null,
    liveLifecycleOwnedGatewayProcesses: () => env.hasGatewayProcessExited(child) ? [] : [child],
    gatewayProcessDiagnostics: new WeakMap(), gatewayHardTerminatedProcesses: new WeakSet(),
    gatewayProcessTreeChildren: new WeakSet(dev ? [child] : []),
    gatewayProcessTreeTerminations: new Map(),
    quitFromSignal: false, quitUnconfirmedProcessTrees: new Set([child]),
    gatewayProcessCleanupContexts: new Map([[child, launch]]),
    setAppExitPhase: phase => { env.appExitPhase = phase }, setQuitStatus() {},
    artifactPreviewLeaseBroker: { shutdown: async () => {} },
    nativeWorkbenchSurfaces: { destroyAll: async () => {} },
    quitOwnedGateway: async () => { calls.drains += 1; return false },
    drainOwnedGatewayForLegacyQuit: async () => false,
    desktopT: key => key, publishGatewayConnection() {},
    desktopGatewayConnectionSuspendedForExit: () => false,
    dialog: { showErrorBox: (...args) => calls.errors.push(args) },
    app: { exit: code => calls.exits.push(code) },
    desktopReliabilityTelemetry: { finishSession() {} }, destroyWindowsTray() {},
    desktopBrowser: { close: async () => {} },

  }
  const beforeQuit = new Function('env', `with (env) {
    ${functions.join('\n')}
    ${productionQuit ? productionQuitFunctions.join('\n') : ''}
    return ${beforeQuitHandler}
  }`)(env)
  const request = () => beforeQuit({ preventDefault: () => { calls.prevented += 1 } })
  env.app.quit = request
  const advance = async milliseconds => {
    now += milliseconds
    mock.timers.tick(milliseconds)
    await flush()
    mock.timers.tick(0)
    await flush()
  }
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    await run({ child, launch, calls, env, request, advance, budgets: () => budgets, now: () => now })
  } finally { mock.timers.reset() }
}

for (const platform of ['darwin', 'linux', 'win32']) {
  for (const exited of [false, true]) {
    await scenario(async h => {
      h.request()
      h.request()
      await h.env.quitGatewayDrainPromise
      await flush()
      assert.deepEqual(h.calls.exits, [0], 'failed proof cannot keep a confirmed Quit open')
      assert.equal(h.calls.drains, 1, 'repeated Quit joins the same operation')
      assert.equal(h.env.appExitPhase, 'committed')
      assert.deepEqual(h.calls.errors, [])
      assert.equal(h.calls.signals.length, exited ? 0 : 1)
      if (!exited) assert.deepEqual(h.calls.signals[0], ['child', 'SIGKILL'])
      assert.equal(h.env.gatewayProcessCleanupContexts.get(h.child), h.launch)
      assert.equal(h.env.quitUnconfirmedProcessTrees.has(h.child), true, 'retain unconfirmed proof')
      assert.deepEqual(h.calls.logs.find(row => row.event === 'quit_cleanup_finished'), {
        event: 'quit_cleanup_finished', rootExited: exited, cleanupConfirmed: false,
      })
    }, { platform, exited })
  }
}

for (const platform of ['darwin', 'linux']) {
  for (const exited of [false, true]) {
    await scenario(async h => {
      h.request()
      await h.env.quitGatewayDrainPromise
      await flush()
      assert.deepEqual(h.calls.signals, exited ? [] : [['group', -h.child.pid, 'SIGKILL']],
        'a dev launcher needs its owned group, whose ID must never be reused after root exit')
    }, { platform, exited, dev: true })
  }
}

for (const exited of [false, true]) {
  await scenario(async h => {
    const controller = new AbortController()
    const killer = Object.assign(new EventEmitter(), {
      exitCode: null, signalCode: null,
      kill: signal => { h.calls.signals.push(['taskkill-helper', signal]); return true },
    })
    const promise = terminateWindowsProcessTree({
      pid: h.child.pid, timeoutMs: 5_000, signal: controller.signal,
      spawnProcess: () => killer,
      fallback: () => assert.fail('helper cancellation cannot reuse an old target PID'),
    })
    h.env.gatewayProcessTreeTerminations.set(h.child, { promise, controller })
    h.request()
    await h.env.quitGatewayDrainPromise
    await flush()
    assert.equal(await promise, false)
    assert.deepEqual(h.calls.signals, exited
      ? [['taskkill-helper', 'SIGKILL']]
      : [['taskkill-helper', 'SIGKILL'], ['child', 'SIGKILL']],
    'Quit reaps an earlier taskkill operation before root fallback, including an exited root')
    assert.deepEqual(h.calls.exits, [0])
  }, { platform: 'win32', exited })
}

// Retain the production unsupported-peer branch as well as the outer Quit
// handler. A mocked quitOwnedGateway cannot expose a legacy drain that spends
// the tree-termination allowance waiting for the launcher to exit.
function legacyQuitRuntime(h, { request = 'accepted', helperHangs = false } = {}) {
  const treeCalls = []
  const exitWaiters = new Set()
  let descendantAlive = true
  let shutdownRequests = 0
  const finishRoot = signal => {
    h.child.signalCode = signal
    for (const resolve of [...exitWaiters]) resolve(true)
  }
  h.child.kill = signal => {
    h.calls.signals.push(['child', signal])
    finishRoot(signal)
    return true
  }
  h.env.quitUnconfirmedProcessTrees.clear()
  if (h.env.process.platform === 'win32') h.env.gatewayProcessCleanupContexts.clear()
  Object.assign(h.env, {
    quitGatewayWithinBudget,
    ownedGatewayRecord: child => {
      assert.equal(child, h.child)
      return { instanceNonce: h.launch.instanceNonce }
    },
    requestVerifiedDesktopGatewayQuit: async () => {
      h.calls.drains += 1
      return { kind: 'unsupported' }
    },
    requestOwnedGatewayShutdown: async () => {
      shutdownRequests += 1
      if (request === 'throw') throw new Error('synthetic legacy shutdown transport failure')
      if (request === 'request-hang') return new Promise(() => {})
      return Promise.resolve(request !== 'rejected')
    },
    waitForGatewayProcessExit: (child, timeoutMs = 80_000) => {
      assert.equal(child, h.child)
      if (h.env.hasGatewayProcessExited(child)) return Promise.resolve(true)
      return new Promise(resolve => {
        const complete = exited => {
          clearTimeout(timer)
          exitWaiters.delete(complete)
          resolve(exited)
        }
        const timer = setTimeout(() => complete(false), timeoutMs)
        exitWaiters.add(complete)
      })
    },
    terminateWindowsProcessTree: options => {
      treeCalls.push({ at: h.now(), timeoutMs: options.timeoutMs })
      assert.equal(h.env.hasGatewayProcessExited(h.child), false,
        'the retained launcher must remain live until tree termination starts')
      assert.deepEqual(h.calls.signals, [], 'never kill only the launcher before its descendants')
      return terminateWindowsProcessTree({ ...options, spawnProcess: (command, args, settings) => {
        assert.equal(command, 'taskkill')
        assert.deepEqual(args, ['/pid', String(h.child.pid), '/t', '/f'])
        assert.equal(settings.windowsHide, true)
        const killer = Object.assign(new EventEmitter(), {
          exitCode: null, signalCode: null,
          kill(signal) {
            h.calls.signals.push(['taskkill-helper', signal])
            this.signalCode = signal
            return true
          },
        })
        if (!helperHangs) setTimeout(() => {
          descendantAlive = false
          finishRoot('SIGKILL')
          killer.exitCode = 0
          killer.emit('exit', 0, null)
        }, 100)
        return killer
      } })
    },
    hardTerminateGatewayProcess: () => assert.fail('ordinary legacy Quit must use its shared tree budget'),
    clearKnownOwnedGatewayPidFile: async () => {},
    GATEWAY_HARD_KILL_BACKSTOP_MS: 5_000,
    GATEWAY_SHUTDOWN_KILL_AFTER_MS: 75_000,
  })
  return {
    treeCalls,
    shutdownRequests: () => shutdownRequests,
    descendantAlive: () => descendantAlive,
    finishGracefully: () => { descendantAlive = false; finishRoot('SIGTERM') },
  }
}

for (const request of ['accepted', 'rejected', 'throw', 'request-hang']) {
  await scenario(async h => {
    const runtime = legacyQuitRuntime(h, { request })
    h.request()
    await flush()
    assert.equal(runtime.shutdownRequests(), 1)
    if (request === 'accepted' || request === 'request-hang') {
      await h.advance(9_999)
      assert.deepEqual(runtime.treeCalls, [])
      assert.deepEqual(h.calls.exits, [])
      await h.advance(1)
      assert.deepEqual(runtime.treeCalls, [{ at: 10_000, timeoutMs: 5_000 }],
        `${request}: reserve the final five seconds for the retained Windows process tree`)
    } else {
      assert.equal(runtime.treeCalls.length, 1, `${request}: failed legacy shutdown must still clean the owned tree`)
      assert.equal(runtime.treeCalls[0].at, 0)
      assert.equal(runtime.treeCalls[0].timeoutMs, 15_000)
    }
    assert.deepEqual(h.calls.exits, [], 'wait for the tree helper before exiting Electron')
    await h.advance(100)
    await h.env.quitGatewayDrainPromise
    await flush()
    assert.equal(runtime.descendantAlive(), false)
    assert.equal(h.env.hasGatewayProcessExited(h.child), true)
    assert.deepEqual(h.calls.signals, [], 'successful tree cleanup needs no direct launcher fallback')
    assert.deepEqual(h.calls.exits, [0])
    assert.equal(h.calls.drains, 1)
    assert.deepEqual(h.calls.logs.filter(row => row.event === 'quit_finalize_failed'), [])
  }, { platform: 'win32', dev: true, productionQuit: true })
}

await scenario(async h => {
  const runtime = legacyQuitRuntime(h)
  h.request()
  await flush()
  await h.advance(1_000)
  runtime.finishGracefully()
  await h.env.quitGatewayDrainPromise
  await flush()
  assert.deepEqual(runtime.treeCalls, [], 'a fast legacy graceful exit needs no taskkill')
  assert.deepEqual(h.calls.signals, [])
  assert.deepEqual(h.calls.exits, [0])
}, { platform: 'win32', dev: true, productionQuit: true })

await scenario(async h => {
  const runtime = legacyQuitRuntime(h, { helperHangs: true })
  h.request()
  await flush()
  await h.advance(10_000)
  assert.deepEqual(runtime.treeCalls, [{ at: 10_000, timeoutMs: 5_000 }])
  await h.advance(4_999)
  assert.deepEqual(h.calls.exits, [])
  await h.advance(1)
  await h.env.quitGatewayDrainPromise
  await flush()
  assert.deepEqual(h.calls.exits, [0], 'a wedged taskkill still cannot exceed the total Quit deadline')
  assert.deepEqual(h.calls.signals, [['taskkill-helper', 'SIGKILL'], ['child', 'SIGKILL']])
  assert.equal(runtime.descendantAlive(), true, 'root fallback is not evidence that descendants exited')
  assert.equal(h.env.quitUnconfirmedProcessTrees.has(h.child), true)
}, { platform: 'win32', dev: true, productionQuit: true })

for (const platform of ['darwin', 'linux', 'win32']) {
  await scenario(async h => {
    const runtime = legacyQuitRuntime(h)
    h.env.quitFromSignal = true
    h.request()
    await flush()
    await h.advance(15_000)
    assert.deepEqual(h.calls.exits, [], 'signal-triggered legacy shutdown retains its full graceful drain')
    assert.deepEqual(runtime.treeCalls, [])
    runtime.finishGracefully()
    await h.env.quitGatewayDrainPromise
    await flush()
    assert.deepEqual(h.calls.exits, [0])
    assert.deepEqual(h.calls.signals, [])
  }, { platform, dev: true, productionQuit: true })
}

for (const platform of ['darwin', 'linux']) {
  await scenario(async h => {
    const runtime = legacyQuitRuntime(h)
    h.request()
    await flush()
    await h.advance(10_000)
    assert.deepEqual(runtime.treeCalls, [], 'POSIX legacy drain retains its existing policy')
    assert.deepEqual(h.calls.signals, [])
    assert.deepEqual(h.calls.exits, [])
    runtime.finishGracefully()
    await h.env.quitGatewayDrainPromise
    await flush()
    assert.deepEqual(h.calls.exits, [0])
    assert.deepEqual(h.calls.signals, [])
  }, { platform, dev: true, productionQuit: true })
}

for (const failure of ['legacy-hang', 'helper-hang', 'throw', 'preview-throw', 'finalize-throw', 'diagnostic-throw']) {
  await scenario(async h => {
    let completeLate
    if (failure.endsWith('hang')) {
      h.env.quitOwnedGateway = (_child, budget) => {
        h.calls.drains += 1
        const stuck = () => new Promise(resolve => { completeLate = resolve })
        if (failure === 'helper-hang') return stuck()
        return quitGatewayWithinBudget({
          budget, hasExited: () => false, requestQuit: async () => ({ kind: 'unsupported' }),
          legacyDrain: stuck, onLegacyDrain() {},
          waitForExit: () => assert.fail('legacy owns its wait'),
          terminate: () => assert.fail('legacy owns its termination'), onTerminating() {},
        })
      }
    } else if (failure === 'throw') {
      h.env.quitOwnedGateway = () => { h.calls.drains += 1; throw new Error('synthetic cleanup failure') }
    } else if (failure === 'preview-throw') {
      h.env.artifactPreviewLeaseBroker.shutdown = () => { throw new Error('synthetic preview failure') }
    } else if (failure === 'diagnostic-throw') {
      h.env.desktopLog = event => {
        if (event === 'quit_cleanup_finished') throw new Error('synthetic diagnostic failure')
      }
    } else {
      h.env.desktopReliabilityTelemetry.finishSession = () => { throw new Error('synthetic finalization failure') }
    }
    h.request()
    await flush()
    if (completeLate) {
      await h.advance(14_999)
      assert.deepEqual(h.calls.exits, [])
      h.request()
      await h.advance(1)
    }
    await h.env.quitGatewayDrainPromise
    await flush()
    assert.deepEqual(h.calls.exits, [0], failure)
    assert.equal(h.calls.drains, 1, 'another local cleanup failure must not skip Gateway cleanup')
    assert.equal(h.calls.signals.length, 1, 'deadline exhaustion must not skip the final signal')
    if (completeLate) {
      completeLate(true)
      await flush()
      assert.deepEqual(h.calls.exits, [0], 'late cleanup cannot commit a second exit')
    }
  })
}

await scenario(async h => {
  const other = { pid: 23456, exitCode: null, signalCode: null,
    kill: signal => { h.calls.signals.push(['other-child', signal]); return true } }
  let finishOther
  h.env.liveLifecycleOwnedGatewayProcesses = () => [h.child, other]
  h.env.quitOwnedGateway = child => {
    h.calls.drains += 1
    if (child === h.child) throw new Error('synthetic first-child failure')
    return new Promise(resolve => { finishOther = resolve })
  }
  h.request()
  await flush()
  assert.equal(h.calls.drains, 2)
  await h.advance(5_000)
  assert.deepEqual(h.calls.exits, [], 'one failed child must not abort another child cleanup')
  finishOther(true)
  await h.env.quitGatewayDrainPromise
  await flush()
  assert.deepEqual(h.calls.exits, [0])
  assert.equal(h.calls.signals.length, 2)
})

await scenario(async h => {
  let finishWriter
  h.env.desktopWriters.activeCount = 1
  h.env.waitForDesktopWriterOperations = () => new Promise(resolve => { finishWriter = resolve })
  h.request()
  await h.advance(60_000)
  assert.equal(h.budgets(), 0, 'the Quit budget starts only after protected writes settle')
  assert.equal(h.calls.drains, 0)
  assert.deepEqual(h.calls.exits, [])
  h.env.desktopWriters.activeCount = 0
  finishWriter()
  await flush()
  await h.env.quitGatewayDrainPromise
  assert.equal(h.budgets(), 1)
  assert.deepEqual(h.calls.exits, [0])
})

await scenario(async h => {
  h.env.updateApplying = true
  h.request()
  await h.advance(60_000)
  assert.equal(h.budgets(), 0, 'an update owns handoff; ordinary Quit cannot bypass it')
  assert.equal(h.env.quitRequestedDuringUpdateDrain, true)
  assert.deepEqual(h.calls.exits, [])
})

await scenario(async h => {
  h.env.quitFromSignal = true
  h.request()
  await h.env.quitGatewayDrainPromise
  await flush()
  assert.deepEqual(h.calls.exits, [], 'signals retain their existing failure policy')
  assert.equal(h.calls.errors.length, 1)
  assert.deepEqual(h.calls.signals, [])
})

// Execute the native fixture's own host construction on every platform. A
// separate unit-test environment cannot detect missing globals in that host.
{
  const nativeFixture = ts.createSourceFile('fixture.mjs', readFileSync(
    new URL('./fixtures/desktop-quit-acceptance/main.mjs', import.meta.url), 'utf8',
  ), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
  const environmentFactory = nativeFixture.statements.find(node =>
    ts.isFunctionDeclaration(node) && node.name?.text === 'makeEnvironment')
  const requiredDeclaration = nativeFixture.statements.filter(ts.isVariableStatement)
    .flatMap(node => [...node.declarationList.declarations])
    .find(node => node.name.getText(nativeFixture) === 'required')
  assert.ok(environmentFactory)
  assert.ok(ts.isArrayLiteralExpression(requiredDeclaration?.initializer))
  const required = requiredDeclaration.initializer.elements.map(node => {
    assert.ok(ts.isStringLiteral(node))
    return node.text
  })
  const productionFunctions = new Map(source.statements.filter(ts.isFunctionDeclaration)
    .filter(node => node.name).map(node => [node.name.text, node.getText(source)]))
  for (const name of required) assert.ok(productionFunctions.has(name))
  for (const platform of ['darwin', 'linux', 'win32']) {
    const logs = [], signals = []
    const task = { pid: 12346, exitCode: null, signalCode: null,
      kill(signal) { signals.push(signal); this.signalCode = signal; return true } }
    const host = {
      env: null, EventEmitter, createContext, runInContext, join,
      process: { platform }, BrowserWindow: class {}, ipcMain: {}, screen: {}, dialog: {},
      app: { exit: () => assert.fail('the fixture must keep its real host open') }, exitOnQuit: false,
      settings: { cleanupFailure: true, locale: 'en', manualNativeErrors: false },
      ledger: { quitRequests: 0, acceptedExit: 0, drain: 0, errorBoxes: [] },
      log: (event, detail = {}) => logs.push({ event, ...detail }), renderOwner() {},
      task, taskAlive: () => task.exitCode === null && task.signalCode === null,
      stopTask: () => assert.fail('the failed-cleanup adapter must leave the final signal to production'),
      owner: null, manager: { destroyAll: async () => {} }, distDir: '/synthetic/dist',
      buildDesktopQuitDialogHtml, normalizeDesktopQuitDialogPalette, parseDesktopQuitDialogResponse,
      DesktopQuitBudget, DesktopQuitConfirmation, DesktopWriterAdmission,
      nativeTheme: { shouldUseDarkColors: false }, mainWindowCloseAction, normalizeGatewayLocale,
      functions: productionFunctions, required, messages: '{}', beforeQuit: beforeQuitHandler,
    }
    const environment = new Function('host', `with (host) {
      ${environmentFactory.getText(nativeFixture)}
      makeEnvironment(); return env;
    }`)(host)
    environment.quitConfirmed = true
    environment.app.quit()
    await environment.quitGatewayDrainPromise
    await flush()
    assert.deepEqual(logs.filter(row => row.event === 'quit_finalize_failed'), [],
      `${platform} native fixture must provide every production quit dependency`)
    assert.deepEqual(signals, ['SIGKILL'])
    assert.equal(environment.appExitPhase, 'committed')
    assert.equal(host.ledger.acceptedExit, 1)
    assert.equal(host.ledger.drain, 1)
    assert.equal(environment.quitUnconfirmedProcessTrees.has(task), true)
  }
}

console.log('desktop bounded quit failure and protected admission checks passed')
