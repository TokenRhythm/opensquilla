import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from '@typescript/typescript6'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { AccountError, pauseAccountsForMaintenance } from '../dist/tokenrhythm-account.js'

const source = ts.createSourceFile('main.js', readFileSync(
  new URL('../dist/main.js', import.meta.url), 'utf8',
), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
const functions = new Map(source.statements.filter(ts.isFunctionDeclaration)
  .filter(node => node.name).map(node => [node.name.text, node.getText(source)]))

function deferred() {
  let resolve, reject
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

function harness({ installFails = false, gatewayFails = false, timeoutMs = 1000 } = {}) {
  const disk = deferred()
  const calls = { paused: 0, resumed: 0, installs: 0, gateways: 0, restarts: 0, dialogs: [] }
  const env = {
    AccountError, pauseAccountsForMaintenance,
    AbortSignal: { timeout: () => AbortSignal.timeout(timeoutMs) },
    desktopAccounts: new Map([['profile', { account: {
      pauseForMaintenance() {
        calls.paused++
        return { drained: disk.promise, resume() { calls.resumed++ } }
      },
    } }]]),
    desktopWriters: new DesktopWriterAdmission(),
    windowsInstallerActionsSupported: () => false,
    updateApplying: false, isQuitting: false, mockDownloadedUpdate: false,
    downloadedUpdateVersion: 'synthetic-new', updateInstallHandoffReady: false,
    quitRequestedDuringUpdateDrain: false, updateGatewayShutdownProcess: null,
    desktopUpdateInstallMode: () => 'native',
    setAppExitPhase() {}, createApplicationMenu() {}, createWindowsTray() {},
    setDesktopUpdateState() {}, desktopStartupLog() {}, desktopLog() {}, desktopT: key => key,
    app: { getVersion: () => 'synthetic-old' },
    desktopReliabilityTelemetry: { recordUpdateResult() {}, markUpdateHandoff() {}, clearUpdateHandoff() {} },
    liveLifecycleOwnedGatewayProcesses: () => [],
    stopOwnedGatewaysForUpdate: async () => {
      calls.gateways++
      if (gatewayFails) throw new Error('synthetic gateway failure')
      return true
    },
    autoUpdater: { quitAndInstall() {
      calls.installs++
      if (installFails) throw new Error('synthetic installer failure')
    } },
    showUpdateDialog: async options => { calls.dialogs.push(options) },
    openOrResumeDesktopApp: async () => { calls.restarts++ },
  }
  env.waitForDesktopWriterOperations = () => env.desktopWriters.waitForAtMost(0)
  const apply = new Function('env', `with (env) {
    ${functions.get('restoreDownloadedUpdateRetryState')}
    return ${functions.get('applyDownloadedUpdate')};
  }`)(env)
  return { calls, env, disk, apply }
}

test('native updater waits for account writes before stopping Gateway or handing off', async () => {
  const h = harness()
  const updating = h.apply()
  await new Promise(setImmediate)
  assert.equal(h.calls.paused, 1)
  assert.equal(h.calls.gateways, 0)
  assert.equal(h.calls.installs, 0)
  h.disk.resolve()
  await updating
  assert.equal(h.calls.installs, 1)
  assert.equal(h.calls.resumed, 0, 'admission stays closed after updater commitment')
  assert.equal(h.env.updateInstallHandoffReady, true)
})

for (const failure of ['persistence', 'timeout', 'installer', 'gateway']) {
  test(`native updater restores account admission after ${failure} failure`, async () => {
    const h = harness({ timeoutMs: failure === 'timeout' ? 20 : 1000,
      installFails: failure === 'installer', gatewayFails: failure === 'gateway' })
    const updating = h.apply()
    if (failure === 'persistence') h.disk.reject(new Error('synthetic disk failure'))
    else if (failure !== 'timeout') h.disk.resolve()
    // Keep the test event loop alive for AbortSignal.timeout's unref'ed timer.
    const keepAlive = setTimeout(() => {}, 2000)
    try { await updating } finally { clearTimeout(keepAlive); h.disk.resolve() }
    assert.equal(h.calls.resumed, 1)
    assert.equal(h.env.desktopWriters.closed, false)
    assert.equal(h.env.updateApplying, false)
    assert.equal(h.env.isQuitting, false)
    assert.equal(h.env.downloadedUpdateVersion, 'synthetic-new')
    assert.equal(h.env.updateInstallHandoffReady, false)
    assert.equal(h.calls.installs, failure === 'installer' ? 1 : 0)
    assert.equal(h.calls.dialogs.length, 1)
  })
}
