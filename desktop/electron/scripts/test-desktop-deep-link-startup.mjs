import { strict as assert } from 'node:assert'
import { EventEmitter } from 'node:events'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'

import { desktopDeepLinkArguments, parseDesktopDeepLinkTarget } from '../dist/desktop-deep-link.js'

// Exercise the actual main-process startup wiring without launching a Gateway
// or using the developer's desktop profile. Parser tests alone cannot catch a
// platform excluded from cold-start dispatch or repeated second-instance sends.
const main = readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8')
function between(start, end) {
  const from = main.indexOf(start)
  assert.notEqual(from, -1, start)
  const to = main.indexOf(end, from)
  assert.notEqual(to, -1, end)
  return main.slice(from, to)
}
const handlers = between('function handleDeepLink(rawUrl', 'function registerDesktopDeepLinkProtocolClient(')
const startup = between('const initialDesktopDeepLinkArguments =', '    void app.whenReady().then(') + '\n}'
const sessionKey = 'agent:main:webchat:synthetic-startup'
const link = `opensquilla://open/session/${encodeURIComponent(sessionKey)}`

function launch(platform, argv, lockResults = [true]) {
  const app = new EventEmitter()
  const calls = { lock: 0, waits: [], quit: 0, dialogs: 0, activations: [], delivered: [], reveals: 0 }
  let now = 0
  app.requestSingleInstanceLock = () => {
    const result = lockResults[Math.min(calls.lock, lockResults.length - 1)]
    calls.lock++
    return result
  }
  app.quit = () => { calls.quit++ }
  const context = createContext({
    app,
    process: { platform, argv, on() {} },
    desktopDeepLinkArguments,
    parseDesktopDeepLinkTarget,
    desktopLog() {},
    desktopProcessStartedAt: 0,
    Date: { now: () => now },
    Atomics: { wait(_signal, _index, _value, milliseconds) { calls.waits.push(milliseconds); now += milliseconds } },
    SharedArrayBuffer,
    Int32Array,
    dialog: { showErrorBox() { calls.dialogs++ } },
    desktopT: key => key,
    loadPersistedDesktopLocale: () => null,
    desktopLocale: 'en',
    activateMainWindow: async source => { calls.activations.push(source) },
    sendPendingDesktopSessionTarget: () => {
      calls.delivered.push(runInContext('pendingDesktopSessionKey', context))
    },
    revealDesktopApp: () => { calls.reveals++ },
  })
  runInContext(`
    let pendingDesktopSessionKey = null
    let pendingDesktopDeepLinkOpen = false
    let desktopDeepLinkActivationReady = false
    ${handlers}
    ${startup}
  `, context)
  return {
    app,
    calls,
    pending: () => runInContext('pendingDesktopSessionKey', context),
    ready: () => runInContext('desktopDeepLinkActivationReady = true; activatePendingDesktopDeepLink()', context),
  }
}

for (const platform of ['linux', 'win32']) {
  const cold = launch(platform, ['OpenSquilla', '--flag', link])
  assert.equal(cold.pending(), sessionKey, `${platform}: cold-start target retained`)
  assert.deepEqual(cold.calls.activations, [], `${platform}: defer until app is ready`)
  assert.equal(cold.ready(), true)
  await Promise.resolve()
  assert.deepEqual(cold.calls.delivered, [sessionKey])
  assert.equal(cold.ready(), false, `${platform}: consume activation once`)

  const forwarding = launch(platform, ['OpenSquilla', link], [false])
  assert.equal(forwarding.calls.lock, 1, `${platform}: only one second-instance delivery`)
  assert.deepEqual(forwarding.calls.waits, [], `${platform}: no five-second protocol retry`)
  assert.equal(forwarding.calls.quit, 1)
  assert.equal(forwarding.calls.dialogs, 0, `${platform}: forwarding is not a launch error`)

  const running = launch(platform, ['OpenSquilla'])
  running.ready()
  running.app.emit('second-instance', {}, ['OpenSquilla', link])
  await Promise.resolve()
  assert.deepEqual(running.calls.delivered, [sessionKey], `${platform}: warm-start session delivery`)
  assert.equal(running.calls.reveals, 0, `${platform}: avoid duplicate normal activation`)
  running.app.emit('second-instance', {}, ['OpenSquilla'])
  assert.equal(running.calls.reveals, 1, `${platform}: normal relaunch still reveals`)

  const relaunch = launch(platform, ['OpenSquilla'], [false, false, true])
  assert.equal(relaunch.calls.lock, 3, `${platform}: keep close/relaunch race recovery`)
  assert.deepEqual(relaunch.calls.waits, [400, 400])
  assert.equal(relaunch.calls.quit, 0)

  const blocked = launch(platform, ['OpenSquilla'], [false])
  assert.equal(blocked.calls.waits.reduce((sum, value) => sum + value, 0), 5_000)
  assert.equal(blocked.calls.dialogs, 1, `${platform}: normal failed launches remain actionable`)
  assert.equal(blocked.calls.quit, 1)
}

const mac = launch('darwin', ['OpenSquilla', link])
assert.equal(mac.pending(), null, 'macOS protocol delivery belongs to open-url')
let prevented = false
mac.app.emit('open-url', { preventDefault() { prevented = true } }, link)
assert.equal(prevented, true)
assert.equal(mac.pending(), sessionKey)
mac.ready()
await Promise.resolve()
assert.deepEqual(mac.calls.delivered, [sessionKey])

console.log('desktop deep-link startup checks passed (Linux, Windows, macOS event wiring)')
