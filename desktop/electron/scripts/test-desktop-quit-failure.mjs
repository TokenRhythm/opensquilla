import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { EventEmitter } from 'node:events'
import { mock } from 'node:test'
import ts from '@typescript/typescript6'

import { DesktopQuitBudget, quitGatewayWithinBudget } from '../dist/desktop-quit.js'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'

// Execute production handlers without Electron or real process termination.
const source = ts.createSourceFile(
  'main.js', readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
)
const functions = []
let beforeQuitHandler
function collect(node) {
  if (ts.isFunctionDeclaration(node)
    && ['showDesktopQuitFailure', 'forceOwnedGatewayForQuit', 'terminateGatewayProcess'].includes(node.name?.text)) {
    functions.push(node.getText(source))
  }
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'app.on'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'before-quit') {
    beforeQuitHandler = node.arguments[1].getText(source)
  }
  ts.forEachChild(node, collect)
}
collect(source)
assert.equal(functions.length, 3)
assert.ok(beforeQuitHandler)
const flush = () => new Promise(resolve => setImmediate(resolve))

async function scenario(run, { platform = 'darwin', exited = false, dev = false } = {}) {
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
    await run({ child, launch, calls, env, request, advance, budgets: () => budgets })
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

console.log('desktop bounded quit failure and protected admission checks passed')
