import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from '@typescript/typescript6'
import { DesktopQuitBudget } from '../dist/desktop-quit.js'

const source = ts.createSourceFile(
  'main.js', readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
)
let beforeQuitHandler
let failureHandler
function collect(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'showDesktopQuitFailure') failureHandler = node.getText(source)
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'app.on'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'before-quit') beforeQuitHandler = node.arguments[1].getText(source)
  ts.forEachChild(node, collect)
}
collect(source)
assert.ok(beforeQuitHandler)
assert.ok(failureHandler)

function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

function harness({ signal = false, budgetMs = 1000 } = {}) {
  const accountDrain = deferred()
  const events = []
  const errors = []
  const env = {
    DesktopQuitBudget: class extends DesktopQuitBudget { constructor() { super(budgetMs / 2, budgetMs / 2) } },
    desktopAccounts: new Map([['profile', {
      account: { close: () => { events.push('account-close'); return accountDrain.promise } },
      proxy: { close: async () => { events.push('proxy-close') } },
    }]]),
    systemSessionEnding: false, updateApplying: false, quitGatewayDrainPromise: null,
    holdQuitForTaskConfirmation: () => false, desktopUpdateCheckScheduler: { stop() {} },
    desktopWriters: { activeCount: 0, close: () => Symbol('quit') },
    quitDeferredForDesktopWriters: false, quitWriterAdmission: null,
    desktopLog() {}, process: { platform: 'darwin' }, isQuitting: false,
    cancelGatewayUnexpectedExitRestart() {}, gatewayProcess: null,
    gatewayState: { owned: false, status: 'stopped' },
    hasGatewayProcessExited: () => true, liveLifecycleOwnedGatewayProcesses: () => [],
    quitFromSignal: signal, quitUnconfirmedProcessTrees: new Set(), gatewayProcessCleanupContexts: new Map(),
    setAppExitPhase: phase => { events.push(phase) }, setQuitStatus() {},
    artifactPreviewLeaseBroker: { shutdown: async () => {} },
    nativeWorkbenchSurfaces: { destroyAll: async () => {} },
    desktopT: key => key, publishGatewayConnection() {},
    dialog: { showErrorBox: (title, detail) => errors.push({ title, detail }) },
    app: { exit: code => events.push(`exit:${code}`) },
    desktopReliabilityTelemetry: { finishSession() {} }, destroyWindowsTray() {},
    desktopBrowser: { close: async () => {} },
  }
  const beforeQuit = new Function('env', `with (env) { ${failureHandler}; return ${beforeQuitHandler}; }`)(env)
  return { env, events, errors, accountDrain, quit: () => beforeQuit({ preventDefault: () => events.push('prevented') }) }
}

for (const signal of [false, true]) {
  test(`quit waits for account persistence without a Gateway (signal=${signal})`, async () => {
    const h = harness({ signal })
    h.quit()
    assert.ok(h.events.includes('prevented'))
    assert.ok(h.events.includes('account-close'))
    assert.ok(h.events.includes('proxy-close'))
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(h.events.includes('exit:0'), false)
    h.accountDrain.resolve()
    await h.env.quitGatewayDrainPromise
    await new Promise(resolve => setImmediate(resolve))
    assert.ok(h.events.includes('exit:0'))
    assert.deepEqual(h.errors, [])
  })
}

test('account timeout retains the app and a later Quit succeeds after persistence settles', async () => {
  const h = harness({ budgetMs: 30 })
  h.quit()
  await h.env.quitGatewayDrainPromise
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.events.includes('exit:0'), false)
  assert.equal(h.env.quitGatewayDrainPromise, null, 'the failed drain remains retryable')
  assert.deepEqual(h.errors, [{ title: 'quit.accountFailed', detail: 'quit.accountFailedDetail' }])
  h.accountDrain.resolve()
  h.quit()
  await h.env.quitGatewayDrainPromise
  await new Promise(resolve => setImmediate(resolve))
  assert.ok(h.events.includes('exit:0'))
})

test('failed account persistence prevents exit and a recovered close can be retried', async () => {
  const h = harness()
  h.quit()
  h.accountDrain.reject(new Error('account storage unavailable'))
  await h.env.quitGatewayDrainPromise
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.events.includes('exit:0'), false)
  assert.equal(h.env.quitGatewayDrainPromise, null)
  assert.deepEqual(h.errors, [{ title: 'quit.accountFailed', detail: 'quit.accountFailedDetail' }])
  h.env.desktopAccounts.get('profile').account.close = async () => {}
  h.quit()
  await h.env.quitGatewayDrainPromise
  await new Promise(resolve => setImmediate(resolve))
  assert.ok(h.events.includes('exit:0'))
})
