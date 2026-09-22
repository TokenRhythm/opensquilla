import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from '@typescript/typescript6'

import { DesktopQuitBudget } from '../dist/desktop-quit.js'

// Execute the shipped failure path without launching Electron or terminating
// real processes. In particular, a dead root must never become a PID retry.
const source = ts.createSourceFile(
  'main.js',
  readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.JS,
)
let failureHandler
let beforeQuitHandler
function collect(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'showDesktopQuitFailure') {
    failureHandler = node.getText(source)
  }
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'app.on'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'before-quit') {
    beforeQuitHandler = node.arguments[1].getText(source)
  }
  ts.forEachChild(node, collect)
}
collect(source)
assert.ok(failureHandler, 'Expected production quit failure handler.')
assert.ok(beforeQuitHandler, 'Expected production before-quit handler.')

const child = { pid: 12345 }
let rootExited = false
let quitAttempts = 0
let preventions = 0
const failures = []
const statuses = []
const unexpected = () => assert.fail('Failed cleanup must not commit exit or signal a dead root.')
const env = {
  DesktopQuitBudget,
  systemSessionEnding: false,
  updateApplying: false,
  quitGatewayDrainPromise: null,
  holdQuitForTaskConfirmation: () => false,
  desktopUpdateCheckScheduler: { stop() {} },
  desktopWriters: { activeCount: 0, close: () => Symbol('quit') },
  quitDeferredForDesktopWriters: false,
  quitWriterAdmission: null,
  desktopLog() {},
  process: { platform: 'win32' },
  isQuitting: false,
  cancelGatewayUnexpectedExitRestart() {},
  gatewayProcess: child,
  gatewayState: { owned: true, status: 'ready' },
  hasGatewayProcessExited: value => {
    assert.equal(value, child)
    return rootExited
  },
  liveLifecycleOwnedGatewayProcesses: () => rootExited ? [] : [child],
  quitFromSignal: false,
  quitUnconfirmedProcessTrees: new Set([child]),
  setAppExitPhase: phase => { statuses.push(phase) },
  setQuitStatus: key => { statuses.push(key) },
  artifactPreviewLeaseBroker: { shutdown: async () => {} },
  nativeWorkbenchSurfaces: { destroyAll: async () => {} },
  quitOwnedGateway: async value => {
    assert.equal(value, child)
    assert.equal(rootExited, false, 'Never retry an exited root PID.')
    quitAttempts += 1
    return false
  },
  desktopT: key => key,
  publishGatewayConnection() {},
  dialog: { showErrorBox: (title, detail) => failures.push({ title, detail }) },
  app: { exit: unexpected },
  desktopReliabilityTelemetry: { finishSession: unexpected },
  destroyWindowsTray: unexpected,
  desktopBrowser: { close: unexpected },
  stopGateway: unexpected,
}
const beforeQuit = new Function('env', `with (env) {
  ${failureHandler}
  return ${beforeQuitHandler}
}`)(env)

async function attemptQuit() {
  const previousFailures = failures.length
  beforeQuit({ preventDefault: () => { preventions += 1 } })
  for (let attempt = 0; attempt < 100 && failures.length === previousFailures; attempt += 1) {
    await new Promise(resolve => setImmediate(resolve))
  }
  assert.equal(failures.length, previousFailures + 1)
  assert.equal(env.quitGatewayDrainPromise, null)
  assert.equal(env.isQuitting, true)
  assert.equal(env.gatewayState.status, 'error')
  assert.deepEqual([...env.quitUnconfirmedProcessTrees], [child], 'Keep unconfirmed cleanup evidence.')
}

await attemptQuit()
assert.deepEqual(failures.at(-1), { title: 'quit.failed', detail: 'quit.failedDetail' })
assert.equal(quitAttempts, 1, 'A still-live owned root may be retried.')

// The root can exit after a failed taskkill, for example through its watchdog.
rootExited = true
for (let attempt = 0; attempt < 2; attempt += 1) {
  await attemptQuit()
  assert.deepEqual(failures.at(-1), {
    title: 'quit.manualRecovery', detail: 'quit.manualRecoveryDetail',
  })
  assert.equal(env.gatewayState.error, 'quit.manualRecovery')
  assert.equal(statuses.at(-1), 'quit.manualRecovery')
  assert.equal(quitAttempts, 1, 'Repeated Quit cannot reuse the dead root PID.')
}
assert.equal(preventions, 3)
assert.equal(statuses.includes('committed'), false)

console.log('desktop quit failure recovery checks passed')
