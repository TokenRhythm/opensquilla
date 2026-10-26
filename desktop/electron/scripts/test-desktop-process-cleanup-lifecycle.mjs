import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import ts from '@typescript/typescript6'

import {
  DESKTOP_QUIT_TERMINATION_MS, DesktopQuitBudget, DesktopQuitConfirmation, quitGatewayWithinBudget,
} from '../dist/desktop-quit.js'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { stopAndJoinLifecycleProcesses } from '../dist/gateway-lifecycle.js'
import {
  evaluateFirstSendEvidence, parseEvidenceLog, summarizeDesktopLog,
} from './packaged-first-send-evidence.mjs'

const source = ts.createSourceFile(
  'main.js', readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
)
const names = new Set([
  'confirmOwnedGatewayProcessCleanup', 'terminateOwnedGatewayForQuit', 'quitOwnedGateway',
  'retireStoppedGatewayProcessCleanup',
  'stopAndJoinAllLifecycleOwnedGateways',
  'joinGatewayProcessRetirements', 'confirmStoppedGatewayProcessScopes',
  'holdQuitForTaskConfirmation',
])
const functions = []
let beforeQuitSource
let failureSource
let retirementHandler
let replacementGuard
let importGuard
function collect(node) {
  if (ts.isFunctionDeclaration(node)) {
    if (names.has(node.name?.text)) functions.push(node.getText(source))
    if (node.name?.text === 'showDesktopQuitFailure') failureSource = node.getText(source)
  }
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'app.on'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'before-quit') {
    beforeQuitSource = node.arguments[1].getText(source)
  }
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'child.once'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'exit'
    && node.arguments[1].getText(source).includes('retireStoppedGatewayProcessCleanup(child)')) {
    retirementHandler = node.arguments[1].getText(source)
  }
  if (ts.isIfStatement(node) && node.expression.getText(source) === '!await joinGatewayProcessRetirements()') {
    replacementGuard = node.getText(source)
  }
  if (ts.isIfStatement(node) && node.expression.getText(source) === '!await confirmStoppedGatewayProcessScopes()') {
    importGuard = node.getText(source)
  }
  ts.forEachChild(node, collect)
}
collect(source)
assert.equal(functions.length, names.size)
assert.ok(beforeQuitSource)
assert.ok(failureSource)
assert.ok(retirementHandler)
assert.ok(replacementGuard)
assert.ok(importGuard)

function deferred() {
  let resolve
  const promise = new Promise(accept => { resolve = accept })
  return { promise, resolve }
}

function harness({ exited = false, platform = 'darwin', complete = true } = {}) {
  const child = { pid: 12345, exitCode: exited ? 0 : null, signalCode: null }
  const launch = { instanceNonce: 'retained-private-nonce', ownershipDir: '/synthetic/retained-profile' }
  const order = []
  const logs = []
  const env = {
    process: { platform },
    DesktopQuitBudget, DESKTOP_QUIT_TERMINATION_MS,
    appExitPhase: 'running', isQuitting: false, updateApplying: false, gatewayStoppingProcesses: new Set([child]),
    gatewayProcess: child, gatewayState: { owned: true }, stopAndJoinLifecycleProcesses,
    restartAllowed: true, transitionGatewayConnection: () => order.push('profile-error'),
    cancelGatewayUnexpectedExitRestart() {}, desktopStartupLog() {},
    stopGateway() { assert.fail('this synthetic join begins after root exit') },
    liveLifecycleOwnedGatewayProcesses: () => env.gatewayProcess
      && !env.hasGatewayProcessExited(env.gatewayProcess) ? [env.gatewayProcess] : [],
    gatewayProcessCleanupContexts: new Map(platform === 'win32' ? [] : [[child, launch]]),
    gatewayProcessCleanupConfirmed: new WeakSet(),
    gatewayProcessCleanupOperations: new WeakMap(),
    gatewayProcessCleanupRetirements: new Map(),
    quitUnconfirmedProcessTrees: new Set(),
    gatewayHardTerminatedProcesses: new WeakSet(),
    gatewayProcessTreeTerminations: new WeakMap(),
    quitGatewayWithinBudget,
    hasGatewayProcessExited: value => value.exitCode !== null || value.signalCode !== null,
    desktopLog: (event, detail) => logs.push({ event, ...detail }),
    setQuitStatus() {}, setAppExitPhase() {},
    quitConfirmed: false, quitFromSignal: false, quitConfirmationPromise: null,
    quitConfirmationWantsExit: false, systemSessionEnding: false,
    desktopQuitConfirmation: new DesktopQuitConfirmation(),
    desktopWriters: new DesktopWriterAdmission(), quitWriterAdmission: null,
    waitForDesktopWriterOperations: () => env.desktopWriters.waitForAtMost(0),
    nativeWorkbenchSurfaces: {
      hasBrowserTabs: () => false,
      closeBrowserTabs: async ({ confirm }) => await confirm({ tabCount: 0 }),
    },
    currentMainWindow: () => null, currentOnboardingWindow: () => null,
    desktopT: key => key, app: { quit: () => order.push('quit-confirmed') },
    showDesktopQuitConfirmation: async () => ({ response: 'cancel', remember: false }),
    readVerifiedDesktopGatewayActivity: async () => ({ kind: 'ok', activeCount: 0 }),
    cleanupDesktopProcessTrees: async options => {
      assert.equal(options.launch, launch, 'use retained launch authority, never the current profile')
      order.push('cleanup')
      return complete
    },
    ownedGatewayRecord: () => ({ synthetic: true }),
    requestVerifiedDesktopGatewayQuit: async () => ({ kind: 'quit_accepted' }),
    waitForGatewayProcessExit: async value => env.hasGatewayProcessExited(value),
    terminateGatewayProcess: (value, signal) => {
      assert.equal(value, child)
      assert.equal(env.hasGatewayProcessExited(value), false, 'never re-signal an exited root')
      order.push('kill-root')
      value.signalCode = signal
    },
    terminateWindowsProcessTree: async () => {
      order.push('windows-tree')
      child.exitCode = 0
      return true
    },
    drainOwnedGatewayForLegacyQuit: async () => {
      order.push('legacy-drain')
      child.exitCode = 0
      return true
    },
  }
  const handlers = new Function('env', `with (env) {
    ${functions.join('\n')}
    return {
      terminateOwnedGatewayForQuit, quitOwnedGateway, retireStoppedGatewayProcessCleanup,
      stopAndJoinAllLifecycleOwnedGateways,
      joinGatewayProcessRetirements, confirmStoppedGatewayProcessScopes,
      holdQuitForTaskConfirmation,
      replaceAfterRetirement: async () => { ${replacementGuard}; return true },
      importAfterConfirmation: async () => { ${importGuard}; return true },
      closed: (child, code, signal) => (${retirementHandler})(code, signal),
    };
  }`)(env)
  return { child, launch, order, logs, env, handlers }
}

async function withBudget(test) {
  const budget = new DesktopQuitBudget(100, 1000)
  try { await test(budget) } finally { budget.dispose() }
}

// Feed the production shutdown-request log to the packaged acceptance gate.
// Keep every other field valid, so rejected requests cannot borrow a later
// clean process exit as evidence that graceful shutdown was accepted.
const firstSendFixtures = [0, 1].map(cancellations => JSON.parse(execFileSync(
  process.execPath,
  [fileURLToPath(new URL('./test-packaged-first-send-evidence.mjs', import.meta.url)),
    '--fixture-json', ...(cancellations === 0 ? ['--no-cancellation'] : [])],
  { encoding: 'utf8' },
)).result)
for (const response of [
  { kind: 'quit_accepted', remainingMs: 10_000, totalRemainingMs: 15_000 },
  { kind: 'rejected' },
  { kind: 'unsupported' },
  { kind: 'unreachable' },
]) {
  await withBudget(async budget => {
    const h = harness({ platform: 'win32' })
    const records = []
    h.env.desktopLog = (event, detail) => records.push({ event, ...detail })
    h.env.requestVerifiedDesktopGatewayQuit = async () => response
    h.env.quitGatewayWithinBudget = async ({ requestQuit }) => {
      assert.equal(await requestQuit(10_000), response)
      return true
    }
    assert.equal(await h.handlers.quitOwnedGateway(h.child, budget), true)
    const request = records.find(record => record.event === 'quit_gateway_shutdown_requested')
    assert.deepEqual(request, {
      event: 'quit_gateway_shutdown_requested', pid: h.child.pid, ...response,
      accepted: response.kind === 'quit_accepted', alreadyStopping: false,
    })
    for (const original of firstSendFixtures) {
      const fixture = structuredClone(original)
      assert.deepEqual(evaluateFirstSendEvidence(fixture).failures, [])
      const index = fixture.desktopLog.records.findIndex(
        record => record.event === 'quit_gateway_shutdown_requested',
      )
      fixture.desktopLog.records[index] = { at: fixture.desktopLog.records[index].at, ...request }
      fixture.desktopLog = summarizeDesktopLog(parseEvidenceLog(
        fixture.desktopLog.records.map(record => JSON.stringify(record)).join('\n') + '\n',
      ))
      const failures = evaluateFirstSendEvidence(fixture).failures
      if (response.kind === 'quit_accepted') {
        assert.deepEqual(failures, [])
        assert.equal(fixture.desktopLog.expectedShutdownCancellationCount, fixture.renderer.consoleErrors)
      } else {
        assert.ok(failures.includes('invalid natural Quit evidence'), response.kind)
        assert.equal(fixture.desktopLog.expectedShutdownCancellationCount, 0)
      }
    }
  })
}

for (const platform of ['darwin', 'linux']) {
  await withBudget(async budget => {
    const h = harness({ platform })
    assert.equal(await h.handlers.terminateOwnedGatewayForQuit(h.child, budget), true)
    assert.deepEqual(h.order, ['cleanup', 'kill-root'], 'fence and verify tools before killing the root')
    assert.equal(h.env.gatewayProcessCleanupContexts.size, 0)
    assert.equal(h.env.quitUnconfirmedProcessTrees.size, 0)
  })

  for (const exited of [false, true]) {
    await withBudget(async budget => {
      const h = harness({ platform, exited, complete: false })
      assert.equal(await h.handlers.terminateOwnedGatewayForQuit(h.child, budget), false)
      assert.deepEqual(h.order, ['cleanup'], 'uncertain tool cleanup must not become root-only success')
      assert.equal(h.env.gatewayProcessCleanupContexts.get(h.child), h.launch)
      assert.equal(h.env.quitUnconfirmedProcessTrees.has(h.child), true)
    })
  }

  for (const exitPhase of ['before-request', 'request', 'wait']) {
    await withBudget(async budget => {
      const h = harness({ platform, exited: exitPhase === 'before-request' })
      h.env.requestVerifiedDesktopGatewayQuit = async () => {
        h.order.push('request')
        if (exitPhase === 'request') h.child.exitCode = 0
        return { kind: 'quit_accepted' }
      }
      h.env.waitForGatewayProcessExit = async () => {
        h.order.push('wait')
        h.child.exitCode = 0
        return true
      }
      assert.equal(await h.handlers.quitOwnedGateway(h.child, budget), true)
      assert.deepEqual(h.order, {
        'before-request': ['cleanup'], request: ['request', 'cleanup'], wait: ['request', 'wait', 'cleanup'],
      }[exitPhase], 'natural exit and watchdog exit must still confirm independent tools')
    })
  }
}

await withBudget(async budget => {
  const h = harness({ exited: true, complete: false })
  assert.equal(await h.handlers.quitOwnedGateway(h.child, budget), false)
  h.env.cleanupDesktopProcessTrees = async () => { h.order.push('retry-cleanup'); return true }
  assert.equal(await h.handlers.quitOwnedGateway(h.child, budget), true)
  assert.deepEqual(h.order, ['cleanup', 'retry-cleanup'], 'retained scope permits retry after root death')
  assert.equal(h.env.quitUnconfirmedProcessTrees.size, 0)
})

await withBudget(async budget => {
  const h = harness()
  h.env.requestVerifiedDesktopGatewayQuit = async () => ({ kind: 'unsupported' })
  assert.equal(await h.handlers.quitOwnedGateway(h.child, budget), true)
  assert.deepEqual(h.order, ['legacy-drain'], 'unsupported peers keep the existing full drain')
  assert.equal(h.env.gatewayProcessCleanupContexts.size, 0)
})

await withBudget(async budget => {
  const h = harness({ platform: 'win32' })
  assert.equal(await h.handlers.terminateOwnedGatewayForQuit(h.child, budget), true)
  assert.deepEqual(h.order, ['windows-tree'], 'Windows keeps its existing tree termination')
})

for (const scenario of ['clean-stop', 'crash', 'unexpected-clean-exit', 'unexpected-signal', 'quitting', 'update']) {
  const h = harness({ exited: true })
  if (['crash', 'unexpected-clean-exit', 'unexpected-signal'].includes(scenario)) {
    h.env.gatewayStoppingProcesses.clear()
  }
  h.env.isQuitting = scenario === 'quitting'
  h.env.updateApplying = scenario === 'update'
  const signal = scenario === 'unexpected-signal' ? 'SIGKILL' : null
  h.child.exitCode = signal ? null : scenario === 'crash' ? 1 : 0
  h.child.signalCode = signal
  h.handlers.closed(h.child, h.child.exitCode, h.child.signalCode)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(h.order, scenario === 'clean-stop' ? ['cleanup'] : [],
    'only successful intentional stop verification may retire a scope in the background')
  assert.equal(h.env.gatewayProcessCleanupContexts.has(h.child), scenario !== 'clean-stop')
  assert.equal(h.env.quitUnconfirmedProcessTrees.size, 0, 'background failure is not a committed quit')
}

for (const { code, signal } of [
  { code: null, signal: 'SIGTERM' },
  { code: 143, signal: null },
  { code: null, signal: 'SIGKILL' },
]) {
  for (const complete of [false, true]) {
    const h = harness({ exited: true, platform: 'linux' })
    h.child.exitCode = code
    h.child.signalCode = signal
    h.env.gatewayProcess = null
    const cleanup = deferred()
    h.env.cleanupDesktopProcessTrees = async ({ launch, budget }) => {
      assert.equal(launch, h.launch, 'retirement verifies the stopped launch authority')
      assert.equal(budget.remainingCleanupMs, 0)
      assert.ok(budget.remainingTotalMs > 0 && budget.remainingTotalMs <= DESKTOP_QUIT_TERMINATION_MS,
        'intentional signal exits keep the existing bounded verification budget')
      h.order.push('cleanup')
      return await cleanup.promise
    }
    h.handlers.closed(h.child, code, signal)
    let joined = false
    const join = h.handlers.stopAndJoinAllLifecycleOwnedGateways().then(result => {
      joined = true
      return result
    })
    let replacementSettled = false
    const replacement = h.handlers.replaceAfterRetirement().then(
      value => { replacementSettled = true; return value },
      error => { replacementSettled = true; return error },
    )
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(h.order, ['cleanup'], 'every intentional exit must verify its retained scope')
    assert.equal(joined, false, 'profile writes must await verification after signal or nonzero exit')
    assert.equal(replacementSettled, false, 'replacement startup must await the same proof')
    cleanup.resolve(complete)
    assert.equal(await join, complete)
    const replacementResult = await replacement
    assert.equal(h.env.gatewayProcessCleanupContexts.has(h.child), !complete)
    if (complete) {
      assert.equal(replacementResult, true)
      h.env.gatewayProcess = { pid: 23456, exitCode: null, signalCode: null }
      h.env.gatewayState = { owned: true, status: 'ready' }
    } else {
      assert.match(replacementResult.message, /cleanup could not be confirmed/)
      assert.equal(await h.handlers.stopAndJoinAllLifecycleOwnedGateways(), false,
        'failed verification cannot become an empty successful join on retry')
    }
    assert.equal(h.handlers.holdQuitForTaskConfirmation({ preventDefault() {} }), true)
    await h.env.quitConfirmationPromise
    assert.equal(h.env.quitConfirmed, complete,
      'only verified retirement plus an idle replacement permits unprompted quit')
    const confirmation = h.logs.find(log => log.event === 'quit_confirmation_required')
    assert.equal(confirmation?.activity, complete ? undefined : 'unknown')
  }
}

for (const complete of [false, true]) {
  await withBudget(async budget => {
    const h = harness({ exited: true })
    const cleanup = deferred()
    h.env.cleanupDesktopProcessTrees = async () => { h.order.push('cleanup'); return await cleanup.promise }
    h.handlers.closed(h.child, 0, null)
    let joined = false
    const join = h.handlers.stopAndJoinAllLifecycleOwnedGateways().then(result => { joined = true; return result })
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(joined, false, 'profile writes wait for retirement even when the root was already gone')
    cleanup.resolve(complete)
    assert.equal(await join, complete)
    assert.equal(await h.handlers.stopAndJoinAllLifecycleOwnedGateways(), complete,
      'a failed retirement remains a failed profile-write boundary on repeated attempts')
    if (!complete) {
      h.env.cleanupDesktopProcessTrees = async () => true
      assert.equal(await h.handlers.quitOwnedGateway(h.child, budget), true)
      assert.equal(await h.handlers.stopAndJoinAllLifecycleOwnedGateways(), true,
        'confirmed scoped retry releases the retained retirement failure')
    }
  })
}

for (const complete of [false, true]) {
  const h = harness({ exited: true })
  const cleanup = deferred()
  h.env.cleanupDesktopProcessTrees = async () => { h.order.push('cleanup'); return await cleanup.promise }
  h.handlers.closed(h.child, 0, null)
  let admitted = false
  const replacement = h.handlers.replaceAfterRetirement().then(
    value => { admitted = value; return value }, error => error,
  )
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(admitted, false, 'replacement spawn must wait for the prior retirement helper')
  cleanup.resolve(complete)
  const result = await replacement
  assert.equal(admitted, complete)
  if (!complete) assert.match(result.message, /cleanup could not be confirmed/)
}

for (const complete of [false, true]) {
  const h = harness({ exited: true, complete })
  h.env.isQuitting = true
  h.handlers.closed(h.child, 0, null)
  assert.deepEqual(h.order, [], 'profile import does not start automatic retirement')
  const outcome = await h.handlers.importAfterConfirmation().catch(error => error)
  if (complete) {
    assert.equal(outcome, true)
    assert.deepEqual(h.order, ['cleanup'])
    assert.equal(h.env.gatewayProcessCleanupContexts.size, 0)
  } else {
    assert.match(outcome.message, /cleanup could not be confirmed before the transfer/)
    assert.equal(h.env.restartAllowed, false, 'failed verification refuses migration and replacement startup')
    assert.deepEqual(h.order, ['cleanup', 'profile-error'])
    assert.equal(h.env.gatewayProcessCleanupContexts.size, 1)
  }
}

{
  const h = harness()
  assert.equal(await h.handlers.confirmStoppedGatewayProcessScopes(), false)
  assert.deepEqual(h.order, [], 'profile verification cannot terminate a still-live retained root')
}

{
  const h = harness({ exited: true })
  const earlier = { pid: 34567, exitCode: 0, signalCode: null }
  const earlierLaunch = { instanceNonce: 'synthetic-earlier-scope' }
  h.env.gatewayProcessCleanupContexts.set(earlier, earlierLaunch)
  const verified = []
  h.env.cleanupDesktopProcessTrees = async options => { verified.push(options.launch); return true }
  assert.equal(await h.handlers.confirmStoppedGatewayProcessScopes(), true)
  assert.deepEqual(new Set(verified), new Set([h.launch, earlierLaunch]),
    'profile replacement must verify previous retained launches as well as the last root')
}

{
  const h = harness({ exited: true })
  h.env.gatewayProcessCleanupContexts.clear()
  assert.equal(await h.handlers.confirmStoppedGatewayProcessScopes(), true)
  assert.deepEqual(h.order, [], 'an external Gateway grants no child cleanup authority')
}

await withBudget(async budget => {
  const h = harness({ exited: true })
  const cleanup = deferred()
  h.env.cleanupDesktopProcessTrees = async () => { h.order.push('cleanup'); return await cleanup.promise }
  h.handlers.closed(h.child, 0, null)
  const background = h.env.gatewayProcessCleanupOperations.get(h.child)
  assert.ok(background)
  const quit = h.handlers.quitOwnedGateway(h.child, budget)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(h.order, ['cleanup'], 'a concurrent quit joins the existing cleanup helper')
  assert.equal(h.env.gatewayProcessCleanupOperations.get(h.child), background)
  cleanup.resolve(true)
  assert.equal(await quit, true)
  assert.equal(h.env.gatewayProcessCleanupContexts.size, 0)
})

{
  const h = harness({ exited: true })
  const cleanup = deferred()
  h.env.cleanupDesktopProcessTrees = async () => { h.order.push('cleanup'); return await cleanup.promise }
  h.handlers.closed(h.child, 0, null)
  const background = h.env.gatewayProcessCleanupOperations.get(h.child)
  const budget = new DesktopQuitBudget(10, 10)
  try {
    assert.equal(await h.handlers.quitOwnedGateway(h.child, budget), false)
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(background.budget.remainingTotalMs, 0,
      'an expired foreground quit also cancels the pre-existing helper deadline')
    assert.deepEqual(h.order, ['cleanup'])
  } finally {
    cleanup.resolve(false)
    budget.dispose()
    await background.promise
  }
}

{
  const child = { pid: 4242 }
  const launch = { instanceNonce: 'synthetic-scope' }
  const calls = []
  const env = {
    DesktopQuitBudget, process: { platform: 'darwin' },
    systemSessionEnding: false, updateApplying: false, quitGatewayDrainPromise: null,
    holdQuitForTaskConfirmation: () => false, desktopUpdateCheckScheduler: { stop() {} },
    desktopAccounts: new Map(),
    desktopWriters: { activeCount: 0, close: () => Symbol('quit') },
    quitDeferredForDesktopWriters: false, quitWriterAdmission: null,
    desktopLog() {}, isQuitting: false, cancelGatewayUnexpectedExitRestart() {},
    gatewayProcess: null, gatewayState: { owned: false, status: 'stopped' },
    hasGatewayProcessExited: () => true, liveLifecycleOwnedGatewayProcesses: () => [],
    gatewayProcessCleanupContexts: new Map([[child, launch]]),
    quitFromSignal: false, quitUnconfirmedProcessTrees: new Set([child]),
    setAppExitPhase() {}, setQuitStatus() {}, desktopT: key => key,
    publishGatewayConnection() {},
    artifactPreviewLeaseBroker: { shutdown: async () => {} },
    nativeWorkbenchSurfaces: { hasBrowserTabs: () => false, destroyAll: async () => {} },
    quitOwnedGateway: async value => { assert.equal(value, child); calls.push('cleanup'); return false },
    dialog: { showErrorBox: title => calls.push(title) },
    app: { exit: () => assert.fail('an unconfirmed scope must retain the desktop') },
  }
  const beforeQuit = new Function('env', `with (env) { ${failureSource}; return ${beforeQuitSource}; }`)(env)
  beforeQuit({ preventDefault() {} })
  await env.quitGatewayDrainPromise
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(calls, ['cleanup', 'quit.failed'], 'a dead root remains eligible for scoped retry')
  assert.equal(env.gatewayProcessCleanupContexts.get(child), launch)
}

console.log('desktop process cleanup lifecycle checks passed')
